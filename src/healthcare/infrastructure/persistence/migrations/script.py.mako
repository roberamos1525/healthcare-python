"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
"""

from healthcare.infrastructure.persistence.migrations.history import execute_sql_file

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    """Apply the reviewed SQL file of this revision."""
    execute_sql_file("${up_revision}_up.sql")


def downgrade() -> None:
    """Revert the reviewed SQL file of this revision."""
    execute_sql_file("${up_revision}_down.sql")
