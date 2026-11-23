"""Applies the migrations with the schema owner's connection, adopting schemas created by other backends."""

import logging
from dataclasses import dataclass
from importlib import resources

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from healthcare.infrastructure.persistence.database import parse_connection_string
from healthcare.infrastructure.persistence.migrations.history import HISTORY_SCHEMA, HISTORY_TABLE

_LOGGER = logging.getLogger("healthcare.migrations")


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """What a migration run did."""

    baselined_to: str | None
    version: str | None


def _alembic_config(connection: Connection) -> Config:
    """Build the Alembic configuration for the packaged migration scripts."""
    config = Config()
    config.set_main_option(
        "script_location", str(resources.files("healthcare.infrastructure.persistence").joinpath("migrations"))
    )
    config.attributes["connection"] = connection
    return config


def _baseline_version(connection: Connection) -> str | None:
    """Return the revision already present when another backend migrated the database and Alembic has no history."""
    has_schema, has_history, has_security = connection.execute(
        text(
            f"""
        SELECT to_regclass('identity.users') IS NOT NULL,
               to_regclass('{HISTORY_SCHEMA}.{HISTORY_TABLE}') IS NOT NULL,
               EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ex_appointments_provider_no_overlap')
        """
        )
    ).one()
    if not has_schema or has_history:
        return None
    return "0002" if has_security else "0001"


def _upgrade(connection: Connection) -> MigrationReport:
    """Serialize concurrent runs, baseline an adopted schema and upgrade to the latest revision."""
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('healthcare.schema-migrations'))"))
    baseline = _baseline_version(connection)
    # The history table lives in the platform schema, which must exist before Alembic creates it.
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {HISTORY_SCHEMA}"))
    config = _alembic_config(connection)
    if baseline is not None:
        # Another backend created the schema: record what it applied without running it again.
        command.stamp(config, baseline)
        _LOGGER.info("Existing schema adopted", extra={"version": baseline})
    command.upgrade(config, "head")
    version = connection.execute(text(f"SELECT version_num FROM {HISTORY_SCHEMA}.{HISTORY_TABLE}")).scalar()
    return MigrationReport(baseline, str(version) if version is not None else None)


async def migrate(connection_string: str) -> MigrationReport:
    """Connect as the migration role and apply pending migrations in one transaction."""
    settings = parse_connection_string(connection_string)
    connect_args = {"ssl": settings.ssl} if settings.ssl else {}
    engine = create_async_engine(settings.url, connect_args=connect_args)
    try:
        async with engine.connect() as connection:
            report = await connection.run_sync(_upgrade)
            await connection.commit()
    finally:
        await engine.dispose()
    _LOGGER.info("Database schema is up to date", extra={"version": report.version})
    return report
