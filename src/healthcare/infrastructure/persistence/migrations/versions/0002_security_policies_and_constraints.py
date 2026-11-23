"""V2 tenant isolation, booking integrity and privileges (the reference security script, byte-identical).

Revision ID: 0002
Revises: 0001
"""

from healthcare.infrastructure.persistence.migrations.history import execute_sql_file

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Apply row-level security, the exclusion constraint, the app functions and the grants."""
    execute_sql_file("0002_security_up.sql")


def downgrade() -> None:
    """Remove what V2 created."""
    execute_sql_file("0002_security_down.sql")
