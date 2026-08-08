"""acceptance results and the human gate

C7 of Phase 2. `design.md` §8's ``awaiting_review`` panel shows
acceptance-criteria *results* and offers Approve / Reject-with-feedback, and
§9 settles who produces those results: **not** ``check_acceptance`` — a human.
Neither the per-criterion outcome nor the rejection text exists in any
``events.ndjson``, so neither can live on a derived row.

Two tables, and the interesting decision is what they hang off.

``acceptance_result`` and ``node_review`` are keyed by ``(node_id, attempt)``
    and not by ``run_id``, which is the obvious choice and is the wrong one.
    ``app/storage/replay.py`` deletes the ``run`` row and rebuilds it from the
    log; a foreign key onto ``run`` would either take the reviewer's verdicts
    with it through ``ON DELETE CASCADE`` — invariant 4 says replay may discard
    derived rows and *only* derived rows — or make replay fail on every node
    anybody had reviewed. ``attempt`` survives the rebuild because ``meta.json``
    pins it and replay re-creates the row with the same number.

``node`` is not rebuilt, so both foreign keys point there
    which also means a node deleted while editing a proposal takes its reviews
    with it, and nothing is left orphaned.

Purely additive, like ``dab457c1cd62``: two ``CREATE TABLE``s and nothing else.
No existing table is altered, so no table is rebuilt, so ``usage_event``'s
``usage_event_is_append_only`` trigger cannot be dropped by this revision (see
``a83db6150739`` for why that is the trap worth naming every time).
``tests/storage/test_migrations.py`` proves it against a populated database.

Revision ID: e9f4b9cfa8c1
Revises: dab2c49d6ccb
Create Date: 2026-08-07 21:12:24.839784

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

revision: str = "e9f4b9cfa8c1"
down_revision: str | None = "dab2c49d6ccb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "acceptance_result",
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        # The criterion's text as it stood at run end, copied and not joined:
        # `node.acceptance_criteria` is authored input a human may edit, and a
        # stored position into a list that has since moved describes nothing.
        sa.Column("criterion", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column(
            "outcome",
            sa.Enum("unevaluated", "pass", "fail", name="outcome_enum"),
            nullable=False,
        ),
        sa.Column("created_ms", sa.Integer(), nullable=False),
        sa.Column("updated_ms", sa.Integer(), nullable=False),
        # Named, so a future batch rebuild can re-create it: an anonymous CHECK
        # has no name to come back under and is silently dropped.
        sa.CheckConstraint(
            "outcome IN ('unevaluated', 'pass', 'fail')",
            name=op.f("ck_acceptance_result_criterion_outcome"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node.id"],
            name=op.f("fk_acceptance_result_node_id_node"),
            ondelete="CASCADE",
        ),
        # One row per criterion per attempt, in authored order. The key is also
        # the only index these rows are ever read through: every query is
        # "this node", or "this node, this attempt".
        sa.PrimaryKeyConstraint(
            "node_id", "attempt", "position", name=op.f("pk_acceptance_result")
        ),
    )
    op.create_table(
        "node_review",
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "decision",
            sa.Enum("approved", "rejected", name="decision_enum"),
            nullable=False,
        ),
        # Nullable: an approval usually says nothing, and "" would claim the
        # reviewer typed something and left it blank.
        sa.Column("feedback", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("reviewed_ms", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name=op.f("ck_node_review_review_decision"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["node.id"],
            name=op.f("fk_node_review_node_id_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("node_id", "attempt", name=op.f("pk_node_review")),
    )


def downgrade() -> None:
    # Lossy, and unavoidably so: these rows are authored input with no other
    # copy anywhere. Downgrading past this revision discards every reviewer
    # verdict and every rejection note. Nothing else in the schema refers to
    # them, so the drop is otherwise clean.
    op.drop_table("node_review")
    op.drop_table("acceptance_result")
