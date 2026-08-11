"""persist global AI backend preferences

The row is authored operator configuration, not a projection of a run, so it
correctly lives outside the NDJSON rebuild path. It contains selections only;
credentials remain external to AgentHub.

Revision ID: 3b54e85c2a10
Revises: eb718a7d85f2
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3b54e85c2a10"
down_revision: str | None = "eb718a7d85f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_preference",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("planner_backend", sa.String(), nullable=False),
        sa.Column("planner_harness", sa.String(), nullable=True),
        sa.Column("planner_model", sa.String(), nullable=True),
        sa.Column("search_backend", sa.String(), nullable=False),
        sa.Column("search_harness", sa.String(), nullable=True),
        sa.Column("search_model", sa.String(), nullable=True),
        sa.Column("planner_effort", sa.String(), nullable=False),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "id = 1",
            name=op.f("ck_ai_preference_ai_preference_singleton"),
        ),
        sa.CheckConstraint(
            "planner_backend IN ('api', 'harness')",
            name=op.f("ck_ai_preference_ai_preference_planner_backend"),
        ),
        sa.CheckConstraint(
            "search_backend IN ('api', 'harness')",
            name=op.f("ck_ai_preference_ai_preference_search_backend"),
        ),
        sa.CheckConstraint(
            "planner_effort IN ('low', 'medium', 'high', 'xhigh', 'max')",
            name=op.f("ck_ai_preference_ai_preference_planner_effort"),
        ),
        sa.CheckConstraint(
            "(planner_backend = 'api' AND planner_harness IS NULL "
            "AND planner_model IS NOT NULL) OR "
            "(planner_backend = 'harness' AND planner_harness IS NOT NULL)",
            name=op.f("ck_ai_preference_ai_preference_planner_shape"),
        ),
        sa.CheckConstraint(
            "(search_backend = 'api' AND search_harness IS NULL "
            "AND search_model IS NOT NULL) OR "
            "(search_backend = 'harness' AND search_harness IS NOT NULL)",
            name=op.f("ck_ai_preference_ai_preference_search_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_preference")),
    )


def downgrade() -> None:
    op.drop_table("ai_preference")
