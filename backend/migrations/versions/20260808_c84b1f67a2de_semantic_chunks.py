"""add incremental semantic chunks

Vectors stay in ordinary SQLite rows so schema inspection, migration drift, and
backup remain conventional. sqlite-vec loads only inside the bounded search
worker and ranks these persisted float32 blobs through a temporary vec0 table.

Revision ID: c84b1f67a2de
Revises: f5a1c8e7d294
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84b1f67a2de"
down_revision: str | None = "f5a1c8e7d294"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_source",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
            name=op.f("fk_semantic_source_session_id_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "path", name=op.f("pk_semantic_source")),
    )
    op.create_table(
        "semantic_chunk",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(), nullable=False),
        sa.Column("embedding", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id", "path"],
            ["semantic_source.session_id", "semantic_source.path"],
            name=op.f("fk_semantic_chunk_session_id_semantic_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_semantic_chunk")),
    )
    op.create_index(
        "ix_semantic_chunk_session_path",
        "semantic_chunk",
        ["session_id", "path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_semantic_chunk_session_path", table_name="semantic_chunk")
    op.drop_table("semantic_chunk")
    op.drop_table("semantic_source")
