"""add the incremental code symbol index

The source table records hashes even for files with no tags, allowing restart
to reuse an honestly empty result. Symbol rows cascade through their composite
source key so replacing or deleting one file cannot affect another file's
index entries.

Revision ID: f5a1c8e7d294
Revises: b7c3d9e51f24
Create Date: 2026-08-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f5a1c8e7d294"
down_revision: str | None = "b7c3d9e51f24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "symbol_source",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["session.id"],
            name=op.f("fk_symbol_source_session_id_session"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("session_id", "path", name=op.f("pk_symbol_source")),
    )
    op.create_table(
        "code_symbol",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("source_hash", sa.String(), nullable=False),
        sa.Column("language", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("start_column", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("end_column", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "role IN ('definition', 'reference')",
            name=op.f("ck_code_symbol_code_symbol_role"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id", "path"],
            ["symbol_source.session_id", "symbol_source.path"],
            name=op.f("fk_code_symbol_session_id_symbol_source"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_code_symbol")),
    )
    op.create_index(
        "ix_code_symbol_session_name_role",
        "code_symbol",
        ["session_id", "name", "role"],
        unique=False,
    )
    op.create_index(
        "ix_code_symbol_session_path",
        "code_symbol",
        ["session_id", "path"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_code_symbol_session_path", table_name="code_symbol")
    op.drop_index("ix_code_symbol_session_name_role", table_name="code_symbol")
    op.drop_table("code_symbol")
    op.drop_table("symbol_source")
