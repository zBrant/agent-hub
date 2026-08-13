"""add a per-node code review gate

Existing nodes retain the historical behavior and require review whenever the
session-wide auto-merge bypass is disabled.

Revision ID: c6f4a91d2e73
Revises: 3b54e85c2a10
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6f4a91d2e73"
down_revision: str | None = "3b54e85c2a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("node") as batch_op:
        batch_op.add_column(sa.Column("requires_review", sa.Boolean(), nullable=True))

    op.execute("UPDATE node SET requires_review = 1 WHERE requires_review IS NULL")

    with op.batch_alter_table("node") as batch_op:
        batch_op.alter_column(
            "requires_review",
            existing_type=sa.Boolean(),
            nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("node") as batch_op:
        batch_op.drop_column("requires_review")
