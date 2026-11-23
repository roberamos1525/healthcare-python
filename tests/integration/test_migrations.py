"""Migration scenarios: the migrated schema and the adoption of a schema created by another backend."""

import secrets

from healthcare.infrastructure.persistence.migrate import migrate
from tests.integration.environment import TEST_ROLE_PASSWORD, with_database
from tests.integration.support import env

HISTORY_TABLE = "platform.alembic_version"
LATEST_REVISION = "0002"


async def read_history() -> list[str]:
    """Return the revisions recorded in the migration history of the test database."""
    connection = await env().system()
    try:
        rows = await connection.fetch(f"SELECT version_num FROM {HISTORY_TABLE}")
    finally:
        await connection.close()
    return [str(row["version_num"]) for row in rows]


async def test_the_migrations_create_the_shared_schema_with_row_level_security() -> None:
    """The migrated test database has every table, forced row-level security, the policies and the history."""
    tables = await env().scalar(
        "SELECT count(*) FROM pg_tables WHERE schemaname IN "
        "('identity','organization','provider','patient','scheduling','platform') AND tablename <> 'alembic_version'"
    )
    forced = await env().scalar(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname IN ('organization','provider','patient','scheduling','platform') AND c.relkind = 'r' "
        "AND c.relrowsecurity AND c.relforcerowsecurity"
    )
    policies = await env().scalar("SELECT count(*) FROM pg_policies")
    exclusion = await env().scalar(
        "SELECT count(*) FROM pg_constraint WHERE conname = 'ex_appointments_provider_no_overlap' AND contype = 'x'"
    )
    assert tables == 30 and exclusion == 1 and policies >= 25 and forced >= 20, (
        f"unexpected schema: {tables} tables, {forced} tables with forced RLS, {policies} policies, "
        f"{exclusion} exclusion constraints"
    )
    versions = await read_history()
    assert versions == [LATEST_REVISION], f"unexpected history {versions}"


async def test_a_schema_created_by_another_backend_is_baselined_without_running_the_migrations_again() -> None:
    """A schema without this project's history table is adopted by recording its version, keeping existing data."""
    name = f"healthcare_test_baseline_{secrets.token_hex(6)}"
    admin = await env().admin()
    try:
        await admin.execute(f'CREATE DATABASE "{name}" OWNER healthcare_migrator')
    finally:
        await admin.close()
    try:
        target = await env().admin(name)
        try:
            await target.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        finally:
            await target.close()
        connection = with_database(env().admin_connection, name, "healthcare_migrator", TEST_ROLE_PASSWORD)
        first = await migrate(connection)
        assert first.version == LATEST_REVISION and first.baselined_to is None, f"unexpected first run {first}"
        again = await migrate(connection)
        assert again.version == LATEST_REVISION and again.baselined_to is None, f"a second run changes nothing: {again}"

        owner = await env().admin(name)
        try:
            await owner.execute(f"""
                DROP TABLE {HISTORY_TABLE};
                INSERT INTO platform.outbox_events
                    (id, event_type, aggregate_type, aggregate_id, payload, occurred_at, available_at, attempts)
                VALUES (gen_random_uuid(), 'test.marker', 'test', gen_random_uuid(), '{{}}', now(), now(), 0)
            """)
            adopted = await migrate(connection)
            assert adopted.baselined_to == LATEST_REVISION and adopted.version == LATEST_REVISION, (
                f"unexpected baseline {adopted}"
            )
            markers = await owner.fetchval(
                "SELECT count(*) FROM platform.outbox_events WHERE event_type = 'test.marker'"
            )
            assert markers == 1, f"existing data is kept: {markers}"
        finally:
            await owner.close()
    finally:
        cleanup = await env().admin()
        try:
            await cleanup.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await cleanup.close()
