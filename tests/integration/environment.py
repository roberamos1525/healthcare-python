"""The shared integration test environment.

The PostgreSQL server comes from ``HEALTHCARE_TEST_POSTGRES`` (an administrator connection string) or, when it
is not set, from a Testcontainers PostgreSQL container. Every run creates its own database, applies this
project's migrations, serves the API on a random local port in the test event loop and drops the database
afterwards.
"""

import asyncio
import contextlib
import os
import secrets
import socket
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import asyncpg
import uvicorn

from healthcare.api.app import create_app
from healthcare.infrastructure.bootstrap import Container, build_container
from healthcare.infrastructure.config import Settings, Values, load_settings
from healthcare.infrastructure.email import Message
from healthcare.infrastructure.persistence.database import parse_connection_string
from healthcare.infrastructure.persistence.migrate import migrate

TEST_ROLE_PASSWORD = "test_only_password"


class CapturingSender:
    """Collects outgoing emails so tests can read verification, reset and invitation tokens."""

    def __init__(self) -> None:
        """Start with no messages."""
        self.messages: list[Message] = []

    async def send(self, message: Message) -> None:
        """Store the message instead of sending it."""
        self.messages.append(message)

    async def wait_for(self, to: str, subject: str) -> Message:
        """Wait for the most recent message to an address whose subject contains the text."""
        for _ in range(400):
            for message in reversed(self.messages):
                if message.to.lower() == to.lower() and subject.lower() in message.subject.lower():
                    return message
            await asyncio.sleep(0.025)
        raise AssertionError(f"no email with subject containing {subject!r} was sent to {to}")


def with_database(connection: str, database: str, user: str, password: str) -> str:
    """Return the connection string with another database and role (Npgsql keyword form)."""
    parsed = parse_connection_string(connection)
    host_port = parsed.url.split("@", 1)[1].split("/", 1)[0]
    host, _, port = host_port.rpartition(":")
    return f"Host={host};Port={port};Database={database};Username={user};Password={password}"


def asyncpg_dsn(connection: str) -> str:
    """Return an asyncpg DSN for a connection string."""
    return parse_connection_string(connection).url.replace("postgresql+asyncpg://", "postgresql://")


def free_port() -> int:
    """Return a free local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class _QuietServer(uvicorn.Server):
    """A Uvicorn server that leaves signal handling to the test runner."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        """Install no signal handlers."""
        yield


def _role_statement(role: str) -> str:
    """Create a login role when it does not exist yet."""
    return (
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN "
        f"CREATE ROLE {role} LOGIN PASSWORD '{TEST_ROLE_PASSWORD}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS; "
        "END IF; END $$"
    )


@dataclass
class Environment:
    """The running test environment."""

    admin_connection: str = ""
    database: str = ""
    migrator_connection: str = ""
    app_connection: str = ""
    base_url: str = ""
    container: Container | None = None
    emails: CapturingSender = field(default_factory=CapturingSender)
    _server: _QuietServer | None = None
    _server_task: asyncio.Task[None] | None = None
    _testcontainer: Any = None

    def settings(self, overrides: dict[str, str] | None = None) -> Settings:
        """Return test settings for the test database, with optional overrides."""
        values = {
            "ASPNETCORE_ENVIRONMENT": "Testing",
            "ConnectionStrings:Database": self.app_connection,
            "ConnectionStrings:Migrations": self.migrator_connection,
            "ConnectionStrings:Redis": os.environ.get("HEALTHCARE_TEST_REDIS", ""),
            "Auth:PasswordHashIterations": "1000",
            "Auth:RefreshReuseGraceSeconds": "0",
            "Workers:Enabled": "false",
            "RateLimiting:Enabled": "false",
            "Cors:AllowedOrigins:0": "http://localhost:5173",
            "Serilog:MinimumLevel:Default": "Error",
        }
        values.update(overrides or {})
        return load_settings(Values(values))

    def build(self, overrides: dict[str, str] | None = None) -> Container:
        """Build an application container against the test database."""
        return build_container(self.settings(overrides), sender=self.emails)

    async def admin(self, database: str = "postgres") -> asyncpg.Connection:
        """Connect to a database as the server administrator."""
        dsn = asyncpg_dsn(with_database(self.admin_connection, database, *_admin_credentials(self.admin_connection)))
        return await asyncpg.connect(dsn)

    async def system(self) -> asyncpg.Connection:
        """Connect as the schema owner in system scope, to arrange and inspect data."""
        connection = await asyncpg.connect(asyncpg_dsn(self.migrator_connection))
        await connection.execute("SELECT set_config('app.access_scope', 'system', false)")
        return connection

    async def app_role(self) -> asyncpg.Connection:
        """Connect as the restricted application role with no tenant context."""
        return await asyncpg.connect(asyncpg_dsn(self.app_connection))

    async def scalar(self, sql: str, *args: Any) -> Any:
        """Run a query in system scope and return the first column of the first row."""
        connection = await self.system()
        try:
            return await connection.fetchval(sql, *args)
        finally:
            await connection.close()

    async def execute(self, sql: str, *args: Any) -> None:
        """Run a statement in system scope."""
        connection = await self.system()
        try:
            await connection.execute(sql, *args)
        finally:
            await connection.close()

    async def start(self) -> None:
        """Create the database, apply the migrations and start the API."""
        self.admin_connection = os.environ.get("HEALTHCARE_TEST_POSTGRES", "").strip() or self._start_container()
        self.database = f"healthcare_test_{secrets.token_hex(8)}"
        admin = await self.admin()
        try:
            for statement in (
                _role_statement("healthcare_migrator"),
                _role_statement("healthcare_app"),
                f'CREATE DATABASE "{self.database}" OWNER healthcare_migrator',
            ):
                await admin.execute(statement)
        finally:
            await admin.close()
        target = await self.admin(self.database)
        try:
            await target.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        finally:
            await target.close()
        self.migrator_connection = with_database(
            self.admin_connection, self.database, "healthcare_migrator", TEST_ROLE_PASSWORD
        )
        self.app_connection = (
            with_database(self.admin_connection, self.database, "healthcare_app", TEST_ROLE_PASSWORD)
            + ";Maximum Pool Size=50"
        )
        await migrate(self.migrator_connection)
        self.container = self.build()
        port = free_port()
        config = uvicorn.Config(
            create_app(self.container),
            host="127.0.0.1",
            port=port,
            log_config=None,
            access_log=False,
            server_header=False,
            lifespan="on",
        )
        self._server = _QuietServer(config)
        self._server_task = asyncio.create_task(self._server.serve())
        for _ in range(200):
            if self._server.started:
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            raise RuntimeError("The API did not start.")
        self.base_url = f"http://127.0.0.1:{port}"

    def _start_container(self) -> str:
        """Start a PostgreSQL container when no server is configured (requires Docker)."""
        try:
            from testcontainers.postgres import PostgresContainer
        except ImportError as error:  # pragma: no cover - depends on the workstation
            raise RuntimeError(
                "HEALTHCARE_TEST_POSTGRES is not set and Testcontainers is not installed "
                "(install the 'containers' dependency group and start Docker)."
            ) from error
        container = PostgresContainer("postgres:17-alpine", username="postgres", password="postgres", dbname="postgres")
        container.start()
        self._testcontainer = container
        host, port = container.get_container_host_ip(), container.get_exposed_port(5432)
        return f"Host={host};Port={port};Username=postgres;Password=postgres;Database=postgres"

    async def stop(self) -> None:
        """Stop the API and drop the test database."""
        if self._server is not None and self._server_task is not None:
            self._server.should_exit = True
            await self._server_task
        if self.database:
            admin = await self.admin()
            try:
                await admin.execute(f'DROP DATABASE IF EXISTS "{self.database}" WITH (FORCE)')
            finally:
                await admin.close()
        if self._testcontainer is not None:
            self._testcontainer.stop()


def _admin_credentials(connection: str) -> tuple[str, str]:
    """Return the administrator role and password of the admin connection string."""
    parsed = parse_connection_string(connection)
    credentials = parsed.url.split("://", 1)[1].split("@", 1)[0]
    user, _, password = credentials.partition(":")
    return user or "postgres", password
