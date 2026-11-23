"""The only path to PostgreSQL: a transaction that first applies the caller's row-level security scope.

The application connects as ``healthcare_app``, which owns nothing and cannot bypass row-level security.
Every unit of work is one transaction whose first statement writes ``app.current_organization_id``,
``app.current_user_id`` and ``app.access_scope`` with ``set_config(..., true)``: the values are
transaction-local, so they vanish at commit or rollback and a pooled connection can never carry one
request's tenant into the next.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote, unquote, urlsplit

from sqlalchemy import TextClause, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from healthcare.application.core.caller import Scope
from healthcare.application.core.errors import AppError, conflict, permission_denied, rule

_SCOPE_SQL: Final = text(
    "SELECT set_config('app.current_organization_id', :organization, true), "
    "set_config('app.current_user_id', :user, true), set_config('app.access_scope', :scope, true)"
)

UNIQUE_CONSTRAINTS: Final = {
    "ux_users_email_normalized": ("email_in_use", "An account with this email address already exists."),
    "ux_providers_organization_npi": ("npi_in_use", "A provider with this NPI already exists in the organization."),
    "ux_providers_organization_user": ("user_already_provider", "This user is already linked to a provider profile."),
    "ux_memberships_organization_user": ("already_member", "This person is already a member of the organization."),
    "ux_patient_links_patient_user": ("already_linked", "This account is already linked to the patient."),
    "ux_provider_specialties_provider_name": ("duplicate_specialty", "The same specialty was listed more than once."),
    "ux_clinic_holidays_scope_date": ("holiday_exists", "A holiday is already recorded for this date."),
}


@dataclass(frozen=True, slots=True)
class ConnectionSettings:
    """A parsed connection string."""

    url: str
    pool_size: int
    ssl: str | None


def _npgsql_pairs(text_value: str) -> dict[str, str]:
    """Split an Npgsql ``Key=Value;`` connection string into lower-cased keys."""
    pairs: dict[str, str] = {}
    for item in text_value.split(";"):
        key, separator, value = item.partition("=")
        if separator:
            pairs[key.strip().lower()] = value.strip()
    return pairs


def parse_connection_string(connection_string: str, default_pool_size: int = 100) -> ConnectionSettings:
    """Read an Npgsql-style (``Host=...;Port=...``) or ``postgresql://`` connection string."""
    value = connection_string.strip()
    if not value:
        raise ValueError("The database connection string is empty.")
    pool_size = default_pool_size
    ssl: str | None = None
    if value.startswith(("postgres://", "postgresql://", "postgresql+asyncpg://")):
        parts = urlsplit(value)
        user = unquote(parts.username or "")
        password = unquote(parts.password or "")
        host, port, database = parts.hostname or "localhost", parts.port or 5432, parts.path.lstrip("/")
    else:
        pairs = _npgsql_pairs(value)
        host = pairs.get("host") or pairs.get("server") or "localhost"
        port = int(pairs.get("port") or 5432)
        database = pairs.get("database") or pairs.get("db") or ""
        user = pairs.get("username") or pairs.get("user id") or pairs.get("userid") or pairs.get("user") or ""
        password = pairs.get("password") or pairs.get("pwd") or ""
        size = pairs.get("maximum pool size") or pairs.get("max pool size")
        if size and size.isdigit():
            pool_size = int(size)
        mode = (pairs.get("ssl mode") or pairs.get("sslmode") or "").lower().replace("-", "").replace("_", "")
        ssl = {
            "require": "require",
            "verifyfull": "verify-full",
            "verifyca": "verify-ca",
            "disable": "disable",
            "prefer": "prefer",
            "allow": "allow",
        }.get(mode)
    credentials = quote(user, safe="")
    if password:
        credentials += ":" + quote(password, safe="")
    url = f"postgresql+asyncpg://{credentials}@{host}:{port}/{quote(database, safe='')}"
    return ConnectionSettings(url, pool_size, ssl)


def create_engine(connection_string: str, application_name: str = "healthcare-api") -> AsyncEngine:
    """Create the connection pool of the application role."""
    settings = parse_connection_string(connection_string)
    connect_args: dict[str, Any] = {
        "server_settings": {"application_name": application_name},
        "prepared_statement_cache_size": 512,
    }
    if settings.ssl is not None:
        connect_args["ssl"] = settings.ssl
    return create_async_engine(
        settings.url,
        pool_size=settings.pool_size,
        max_overflow=0,
        pool_timeout=30,
        pool_recycle=1800,
        pool_reset_on_return="rollback",
        connect_args=connect_args,
    )


_TEXT_CACHE: dict[str, TextClause] = {}


def _statement(sql: str) -> TextClause:
    """Return the compiled text clause of a statement, cached by its SQL."""
    statement = _TEXT_CACHE.get(sql)
    if statement is None:
        statement = text(sql)
        _TEXT_CACHE[sql] = statement
    return statement


def scope_parameters(scope: Scope) -> dict[str, str]:
    """Return the three row-level security settings of a scope."""
    return {
        "organization": str(scope.organization_id) if scope.organization_id else "",
        "user": str(scope.user_id) if scope.user_id else "",
        "scope": "system" if scope.system else "tenant",
    }


class SqlTx:
    """A transaction on one pooled connection, bound to a tenant scope."""

    __slots__ = ("connection",)

    def __init__(self, connection: AsyncConnection) -> None:
        """Wrap the connection of an open transaction."""
        self.connection = connection

    async def fetch(self, sql: str, params: Mapping[str, Any] | None = None) -> Sequence[Any]:
        """Run a query and return every row."""
        result = await self.connection.execute(_statement(sql), dict(params or {}))
        return result.fetchall()

    async def fetchrow(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        """Run a query and return its first row, or None."""
        result = await self.connection.execute(_statement(sql), dict(params or {}))
        return result.first()

    async def fetchval(self, sql: str, params: Mapping[str, Any] | None = None) -> Any:
        """Run a query and return the first column of its first row, or None."""
        result = await self.connection.execute(_statement(sql), dict(params or {}))
        return result.scalar()

    async def execute(self, sql: str, params: Mapping[str, Any] | None = None) -> int:
        """Run a statement and return the number of affected rows."""
        result = await self.connection.execute(_statement(sql), dict(params or {}))
        return int(result.rowcount)

    async def set_scope(self, scope: Scope) -> None:
        """Replace the row-level security settings for the rest of the transaction."""
        await self.connection.execute(_SCOPE_SQL, scope_parameters(scope))


def sqlstate(error: BaseException) -> str | None:
    """Return the PostgreSQL error code of a driver error, if any."""
    original = getattr(error, "orig", None)
    for candidate in (original, getattr(original, "__cause__", None)):
        code = getattr(candidate, "sqlstate", None)
        if isinstance(code, str):
            return code
    return None


def constraint_name(error: BaseException) -> str | None:
    """Return the constraint named by a driver error, if any."""
    original = getattr(error, "orig", None)
    for candidate in (getattr(original, "__cause__", None), original):
        name = getattr(candidate, "constraint_name", None)
        if isinstance(name, str):
            return name
    return None


def translate(error: DBAPIError) -> AppError | None:
    """Map constraint violations to application errors with safe messages (None for anything else)."""
    code = sqlstate(error)
    if code == "23P01":
        return conflict("slot_unavailable", "The selected time is no longer available. Choose another time.")
    if code == "23505":
        known = UNIQUE_CONSTRAINTS.get(constraint_name(error) or "")
        if known is not None:
            return conflict(*known)
        return conflict("duplicate_resource", "A record with the same unique value already exists.")
    if code == "23503":
        return rule("invalid_reference", "A referenced record does not exist or is still in use.")
    if code == "23514":
        return rule("constraint_violation", "The request conflicts with a data consistency rule.")
    if code in ("40001", "40P01"):
        return conflict(
            "transient_conflict", "The request could not be completed because of concurrent activity. Try again."
        )
    if code == "42501":
        return permission_denied()
    return None


class SqlDatabase:
    """The application's database: tenant-scoped transactions on a SQLAlchemy asyncpg pool."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Wrap the engine."""
        self.engine = engine

    @asynccontextmanager
    async def transaction(self, scope: Scope) -> AsyncIterator[SqlTx]:
        """Open a transaction, apply the scope, and commit on success or roll back on any error."""
        try:
            async with self.engine.connect() as connection, connection.begin():
                await connection.execute(_SCOPE_SQL, scope_parameters(scope))
                yield SqlTx(connection)
        except DBAPIError as error:
            translated = translate(error)
            if translated is not None:
                raise translated from error
            raise

    async def ping(self) -> None:
        """Run a trivial query (readiness check)."""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def idle_scope(self) -> tuple[str, str, str]:
        """Read the three settings on a pooled connection outside any transaction (diagnostics and tests)."""
        async with self.engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT coalesce(current_setting('app.current_organization_id', true), ''), "
                        "coalesce(current_setting('app.current_user_id', true), ''), "
                        "coalesce(current_setting('app.access_scope', true), '')"
                    )
                )
            ).one()
            await connection.rollback()
        return str(row[0]), str(row[1]), str(row[2])

    async def dispose(self) -> None:
        """Close every pooled connection."""
        await self.engine.dispose()
