"""Names of the migration history table and helpers to run reviewed SQL files inside a migration."""

from importlib import resources
from typing import Final

from alembic import op
from sqlalchemy.util import await_only

HISTORY_SCHEMA: Final = "platform"
HISTORY_TABLE: Final = "alembic_version"


def read_sql(name: str) -> str:
    """Return the text of a reviewed SQL file shipped with the package."""
    return resources.files("healthcare.infrastructure.persistence.migrations").joinpath("sql", name).read_text("utf-8")


def execute_sql_file(name: str) -> None:
    """Run a multi-statement SQL file through asyncpg's simple query protocol in the current transaction.

    SQLAlchemy prepares every statement it sends, and PostgreSQL cannot prepare several commands at once, so
    the file goes to the driver connection directly; it shares the migration's transaction.
    """
    bind = op.get_bind()
    bind.exec_driver_sql("SELECT 1")  # makes sure the driver-level transaction has started
    driver = bind.connection.dbapi_connection
    asyncpg_connection = getattr(driver, "driver_connection", None)
    if asyncpg_connection is None:
        raise RuntimeError("The migrations require the asyncpg driver.")
    await_only(asyncpg_connection.execute(read_sql(name)))
