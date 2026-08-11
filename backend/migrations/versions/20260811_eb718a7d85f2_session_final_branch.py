"""persist the operator-selected final branch

Existing sessions retain the result name the orchestrator historically derived
from their id. New sessions persist the operator's choice instead, while the
existing integration_branch remains an internal, temporary ref.

Revision ID: eb718a7d85f2
Revises: c84b1f67a2de
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "eb718a7d85f2"
down_revision: str | None = "c84b1f67a2de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("session") as batch_op:
        batch_op.add_column(sa.Column("final_branch", sa.String(), nullable=True))

    op.execute(
        "UPDATE session SET final_branch = 'agenthub/' || id || '/result' "
        "WHERE final_branch IS NULL"
    )

    with op.batch_alter_table("session") as batch_op:
        batch_op.alter_column(
            "final_branch",
            existing_type=sa.String(),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("session") as batch_op:
        batch_op.drop_column("final_branch")
