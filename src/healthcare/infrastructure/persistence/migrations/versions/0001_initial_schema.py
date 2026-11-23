"""V1 initial schema: 6 schemas, 30 tables, keys, check constraints and indexes.

Revision ID: 0001
Revises:
"""

from healthcare.infrastructure.persistence.migrations.history import execute_sql_file

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the shared schema from the reviewed SQL file."""
    execute_sql_file("0001_initial_schema_up.sql")


def downgrade() -> None:
    """Drop the schemas created by V1."""
    execute_sql_file("0001_initial_schema_down.sql")
