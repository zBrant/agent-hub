"""persist Phase 3 dashboard history

D5 of Phase 3. Raw one-second psutil samples remain in the bounded in-memory
ring. This additive table stores only mergeable per-minute averages and peaks,
so process restart preserves useful history without turning telemetry into an
unbounded one-row-per-second log.

``node_transition`` is the append-only orchestration activity feed. It is not
an AgentEvent: harnesses do not decide graph merge/review state. Both tables are
independent of run replay. No existing table is rebuilt, so the append-only
usage trigger and every accepted run remain untouched.

Existing nodes receive one honest baseline event only when their current state
is meaningful to the feed. ``node.updated_ms`` was written with that current
status, so the migration can preserve the status and its transition time; it
does not attempt to invent earlier history that the old schema never retained.

Revision ID: b7c3d9e51f24
Revises: e9f4b9cfa8c1
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7c3d9e51f24"
down_revision: str | None = "e9f4b9cfa8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "node_transition",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "ready",
                "running",
                "awaiting_review",
                "blocked",
                "done",
                "failed",
                "skipped",
                name="status_enum",
            ),
            nullable=False,
        ),
        sa.Column("ts", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'running', 'awaiting_review', "
            "'blocked', 'done', 'failed', 'skipped')",
            name=op.f("ck_node_transition_node_status"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node.id"],
            name=op.f("fk_node_transition_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
            name=op.f("fk_node_transition_session_id_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_transition")),
    )
    op.create_index(
        op.f("ix_node_transition_node_id"),
        "node_transition",
        ["node_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_node_transition_session_id"),
        "node_transition",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_node_transition_ts",
        "node_transition",
        ["ts"],
        unique=False,
    )
    op.execute(
        sa.text(
            "INSERT INTO node_transition (session_id, node_id, status, ts) "
            "SELECT session_id, id, status, updated_ms FROM node "
            "WHERE status IN ('awaiting_review', 'blocked', 'done', 'failed', 'skipped')"
        )
    )
    op.execute(
        sa.text(
            "CREATE TRIGGER node_transition_is_append_only "
            "BEFORE UPDATE ON node_transition BEGIN "
            "SELECT RAISE(ABORT, 'node_transition is append-only'); END"
        )
    )
    op.create_table(
        "system_metric_minute",
        sa.Column("minute_ms", sa.Integer(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("cpu_avg_percent", sa.Float(), nullable=False),
        sa.Column("cpu_peak_percent", sa.Float(), nullable=False),
        sa.Column("memory_avg_percent", sa.Float(), nullable=False),
        sa.Column("memory_peak_percent", sa.Float(), nullable=False),
        sa.Column("swap_avg_percent", sa.Float(), nullable=False),
        sa.Column("swap_peak_percent", sa.Float(), nullable=False),
        sa.Column("disk_avg_percent", sa.Float(), nullable=False),
        sa.Column("disk_peak_percent", sa.Float(), nullable=False),
        sa.Column("agent_rss_avg_bytes", sa.Float(), nullable=False),
        sa.Column("agent_rss_peak_bytes", sa.Integer(), nullable=False),
        sa.Column("agent_cpu_avg_percent", sa.Float(), nullable=False),
        sa.Column("agent_cpu_peak_percent", sa.Float(), nullable=False),
        sa.Column("agent_process_count_peak", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("minute_ms", name=op.f("pk_system_metric_minute")),
    )


def downgrade() -> None:
    op.drop_table("system_metric_minute")
    op.execute(sa.text("DROP TRIGGER node_transition_is_append_only"))
    op.drop_index("ix_node_transition_ts", table_name="node_transition")
    op.drop_index(op.f("ix_node_transition_session_id"), table_name="node_transition")
    op.drop_index(op.f("ix_node_transition_node_id"), table_name="node_transition")
    op.drop_table("node_transition")
