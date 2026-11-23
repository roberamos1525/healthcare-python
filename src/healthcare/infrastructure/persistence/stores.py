"""Booking stores built with SQLAlchemy Core: the per-provider advisory lock and in-transaction idempotency."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Text, and_, cast, delete, func, literal, select, text, update
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.exc import DBAPIError

from healthcare.application.core.errors import conflict
from healthcare.application.core.ports import IdempotencyClaim, Tx, new_id
from healthcare.infrastructure.persistence.database import SqlTx, sqlstate
from healthcare.infrastructure.persistence.tables import idempotency_records

_LOCK_TIMEOUT = text("SET LOCAL lock_timeout = '5s'")


def _connection(tx: Tx) -> SqlTx:
    """Return the SQLAlchemy transaction behind the port."""
    if not isinstance(tx, SqlTx):
        raise TypeError("The booking stores need a transaction of the SQL database.")
    return tx


def provider_lock_key(provider_id: UUID) -> str:
    """Return the advisory lock key text of a provider (32 hexadecimal characters, shared by every backend)."""
    return provider_id.hex


class AdvisoryProviderLock:
    """Serializes bookings per provider with ``pg_advisory_xact_lock(hashtextextended(key, 0))``.

    The lock is transaction-scoped (released at commit or rollback) and waiting is capped at five seconds
    with ``SET LOCAL lock_timeout``; a timeout becomes ``409 schedule_busy``.
    """

    async def acquire(self, tx: Tx, provider_id: UUID) -> None:
        """Take the provider's lock for the rest of the transaction."""
        connection = _connection(tx).connection
        await connection.execute(_LOCK_TIMEOUT)
        statement = select(func.pg_advisory_xact_lock(func.hashtextextended(provider_lock_key(provider_id), 0)))
        try:
            await connection.execute(statement)
        except DBAPIError as error:
            if sqlstate(error) == "55P03":
                raise conflict(
                    "schedule_busy", "The provider's schedule is being updated by another request. Try again."
                ) from error
            raise


class SqlIdempotencyStore:
    """Claims idempotency keys with ``INSERT ... ON CONFLICT DO NOTHING`` inside the booking transaction.

    A concurrent request with the same key blocks on the unique index until the first transaction commits
    or rolls back, so both observe a single outcome.
    """

    async def claim(
        self,
        tx: Tx,
        organization_id: UUID,
        user_id: UUID,
        operation: str,
        key: str,
        request_hash: str,
        now: datetime,
        retain_seconds: float,
    ) -> IdempotencyClaim:
        """Insert the key or return what an earlier request with the key stored."""
        connection = _connection(tx).connection
        table = idempotency_records
        same_key = and_(
            table.c.organization_id == organization_id,
            table.c.user_id == user_id,
            table.c.operation == operation,
            table.c.key == key,
        )
        await connection.execute(delete(table).where(same_key, table.c.expires_at <= now))
        inserted = await connection.execute(
            insert(table)
            .values(
                id=new_id(),
                organization_id=organization_id,
                user_id=user_id,
                operation=operation,
                key=key,
                request_hash=request_hash,
                created_at=now,
                expires_at=now + timedelta(seconds=retain_seconds),
            )
            .on_conflict_do_nothing(index_elements=["organization_id", "user_id", "operation", "key"])
        )
        if inserted.rowcount == 1:
            return IdempotencyClaim(is_new=True)
        row = (
            await connection.execute(select(table.c.request_hash, cast(table.c.response_body, Text)).where(same_key))
        ).first()
        if row is None:
            return IdempotencyClaim(is_new=False)
        return IdempotencyClaim(is_new=False, stored_request_hash=row[0], stored_response=row[1])

    async def complete(
        self, tx: Tx, organization_id: UUID, user_id: UUID, operation: str, key: str, response: str, resource_id: UUID
    ) -> None:
        """Record the response of the request that claimed the key."""
        table = idempotency_records
        await _connection(tx).connection.execute(
            update(table)
            .where(
                table.c.organization_id == organization_id,
                table.c.user_id == user_id,
                table.c.operation == operation,
                table.c.key == key,
            )
            .values(response_body=cast(literal(response, Text), JSONB), resource_id=resource_id)
        )
