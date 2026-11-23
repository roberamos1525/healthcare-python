"""Alembic environment: runs the reviewed SQL migrations as the schema owner over asyncpg."""

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from healthcare.infrastructure.persistence.migrations.history import HISTORY_SCHEMA, HISTORY_TABLE


def _run(connection: Connection) -> None:
    """Configure the context on a synchronous connection facade and run the migrations."""
    context.configure(
        connection=connection,
        version_table=HISTORY_TABLE,
        version_table_schema=HISTORY_SCHEMA,
        transaction_per_migration=False,
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: str) -> None:
    """Connect with the migration role and run the migrations in one transaction."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Run the migrations against the database named in the Alembic configuration."""
    connection = context.config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    url = context.config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError("No database URL is configured for the migrations.")
    asyncio.run(_run_async(url))


if context.is_offline_mode():
    raise RuntimeError("Offline (SQL script) mode is not supported; the migrations are already reviewed SQL files.")
run_migrations_online()
