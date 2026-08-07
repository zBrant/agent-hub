"""The core data model (`design.md` §5).

``Session ──1:N── Node ──1:N── Run ──1:N── UsageEvent``, plus
``Node ──N:M── Node`` through :class:`NodeDependency`. No ``Event`` table on
purpose: events live in ``runs/<run_id>/events.ndjson`` and only their
*projection* is here.

No ``Graph`` table either, and that is a deliberate departure from `design.md`
§5's diagram. A ``Graph`` sitting 1:1 between ``Session`` and ``Node`` would
carry no column of its own — the session already owns the objective, the
integration branch and ``auto_merge`` — and would add a join to the one query
the scheduler runs most (:meth:`app.storage.repository.Repository.load_graph`).
**The session row is the graph.**

Reading this file: **invariant 4 is the whole design constraint.** SQLite is a
derived index and NDJSON is the source of truth, so every column has to answer
"which line of the log rebuilds you?". Two kinds of column pass that test, and
they are marked as such throughout:

*authored*
    The input to a run: what the user or the planner asked for, and where it was
    asked to happen. It exists *before* any event does and no amount of
    log-reading can invent it. Replay must therefore never delete a ``session``
    or a ``node`` row — it discards and rebuilds ``run`` and ``usage_event``.
    Node and session transitions remain the orchestrator's responsibility.

*derived*
    Produced by the run and reconstructible from its log. Every one of these
    names the event it comes from. B3's ``agenthub replay <run_id>`` is the
    executable version of that claim.

Deliberately **absent**: token or cost totals on ``run``. Aggregates are
``SUM()`` over ``ix_usage_session_ts`` (`docs/architecture.md` §4), never a
mutable counter that can silently disagree with the rows it summarizes.

Paths are :class:`~pathlib.Path` in Python and ``TEXT`` in SQLite, converted by
:class:`PathType` at the boundary and nowhere else (`docs/conventions.md` §2).
They are stored **absolute**, exactly as written: the row records where the
bytes actually went, so moving ``~/.agenthub`` invalidates them by design rather
than silently resolving to a different file.

Timestamps are int milliseconds UTC supplied by the caller from
:func:`app.models.clock.now_ms`. There is no DB-side ``DEFAULT CURRENT_TIMESTAMP``
anywhere: a replayed row must be able to carry the *event's* timestamp, not the
moment the projection happened to be rebuilt.
"""

import json
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine.interfaces import Dialect
from sqlmodel import Field, SQLModel

from app.models.ids import NodeId, RunId, SessionId
from app.models.status import NodeStatus, RunState, SessionStatus, UsageSource

# SQLite cannot ALTER most things, so Alembic runs in batch mode: it rebuilds the
# table and re-creates its constraints *by name*. An unnamed CHECK or UNIQUE has
# no name to re-create it under and is silently dropped by the second migration.
# Set before any table is declared — the convention is read at Table construction.
SQLModel.metadata.naming_convention = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class PathType(sa.types.TypeDecorator[Path]):
    """``pathlib.Path`` in Python, ``TEXT`` in SQLite. The only path conversion."""

    impl = sa.types.String
    cache_ok = True

    def process_bind_param(self, value: Path | None, dialect: Dialect) -> str | None:
        return None if value is None else str(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Path | None:
        return None if value is None else Path(value)


class StringTupleType(sa.types.TypeDecorator[tuple[str, ...]]):
    """``tuple[str, ...]`` in Python, a JSON array in ``TEXT``.

    A tuple and not a list, on purpose: SQLAlchemy cannot observe an in-place
    ``list.append`` on a JSON-backed column, so the change is silently never
    written. An immutable value has no such failure mode — the only way to
    change it is to replace it, which is an ordinary ``UPDATE``.

    A JSON array is the right shape for :attr:`Node.touches` and the wrong one
    for :class:`NodeDependency`: this is a value read and written with its row
    and never used as a predicate, whereas edges are joined and filtered on
    every scheduler transition. See :class:`NodeDependency` for that argument.
    """

    impl = sa.types.String
    cache_ok = True

    def process_bind_param(
        self, value: Sequence[str] | None, dialect: Dialect
    ) -> str | None:
        return None if value is None else json.dumps(list(value), ensure_ascii=False)

    def process_result_value(
        self, value: Any, dialect: Dialect
    ) -> tuple[str, ...] | None:
        return None if value is None else tuple(str(item) for item in json.loads(value))


def _status(column: str, enum_type: type[Enum]) -> sa.Column[Any]:
    """A status column that reads back as its enum member.

    ``values_callable`` stores the member *value* (``"awaiting_review"``), not
    its Python name (``"AWAITING_REVIEW"``) — the value is what the wire, the
    NDJSON log and `docs/design-system.md` §5 all use, and a database that spells
    it differently makes every hand-written query wrong.

    The vocabulary is enforced by :func:`_status_check` rather than by
    ``sa.Enum(create_constraint=True)``: a type-bound CHECK exists in the
    database but not as a comparable object in the metadata, so autogenerate
    reads it as a constraint someone dropped by hand and proposes removing it on
    every run. An explicit constraint is visible on both sides and the drift
    check stays meaningful.
    """
    return sa.Column(
        column,
        sa.Enum(
            enum_type,
            name=f"{column}_enum",
            create_constraint=False,
            values_callable=lambda members: [member.value for member in members],
        ),
        nullable=False,
    )


def _status_check(
    column: str, enum_type: type[Enum], *, name: str
) -> sa.CheckConstraint:
    """The closed vocabulary, as a named CHECK.

    Named so Alembic's batch mode can re-create it: SQLite rebuilds the table to
    change anything, and an anonymous constraint has no name to come back under.
    This is what stops a typo'd status from reaching the frontend as a state
    with no colour and no icon.
    """
    values = ", ".join(f"'{member.value}'" for member in enum_type)
    return sa.CheckConstraint(f"{column} IN ({values})", name=name)


class Session(SQLModel, table=True):
    """One planning conversation plus its graph (`design.md` §5).

    A session is created before its first run exists, so nothing in any
    ``events.ndjson`` can reconstruct its authored columns. :attr:`status` is a
    projection of node states, but the transition is applied by the orchestrator
    rather than replay storage (`docs/architecture.md` §3).
    """

    __tablename__ = "session"
    __table_args__ = (_status_check("status", SessionStatus, name="session_status"),)

    id: SessionId = Field(sa_type=sa.String, primary_key=True)
    # authored — the objective the user typed.
    title: str
    # authored — the user's repository. Never an agent's cwd (invariant 2).
    repo_path: Path = Field(sa_type=PathType)
    # authored — <workspaces_root>/<session_id>, holding integration/ and the
    # node worktrees. Stored rather than recomputed so a session survives a
    # change of the configured workspaces root.
    workspace_root: Path = Field(sa_type=PathType)
    # authored — agenthub/<session_id>/integration. Also derivable from the id,
    # but a stored ref is the historical truth if the naming scheme ever moves.
    integration_branch: str
    # authored — invariant 6: nothing merges without a human while this is off.
    auto_merge: bool = Field(default=False)
    # derived — a projection of the node states below it.
    status: SessionStatus = Field(
        default=SessionStatus.PLANNING,
        sa_column=_status("status", SessionStatus),
    )
    created_ms: int
    updated_ms: int


class Node(SQLModel, table=True):
    """One activity in the graph (`design.md` §5).

    ``depends_on`` is not a column here: it is :class:`NodeDependency`, one row
    per edge. Everything authored on a node is the planner's or the user's
    brief (`design.md` §8's per-node schema), and nothing on it is derived from
    an event log — a node's *runs* are what the log describes.

    The uniqueness of ``(id, session_id)`` looks redundant next to a primary key
    on ``id`` alone, and it is not decorative: it is the parent key that lets
    :class:`NodeDependency` prove, in SQLite, that both ends of an edge belong
    to the same session.
    """

    __tablename__ = "node"
    __table_args__ = (
        _status_check("status", NodeStatus, name="node_status"),
        # Not a UniqueConstraint: SQLite accepts a unique *index* as an FK
        # parent key, and creating one needs no table rebuild — so an existing
        # database with real runs in it migrates forward without `node` ever
        # being dropped and copied.
        sa.Index("ix_node_id_session_id", "id", "session_id", unique=True),
    )

    id: NodeId = Field(sa_type=sa.String, primary_key=True)
    session_id: SessionId = Field(
        sa_type=sa.String, foreign_key="session.id", ondelete="CASCADE", index=True
    )
    # authored — everything down to `estimated_effort` is the planner's or the
    # user's brief.
    name: str
    prompt: str
    # authored — one entry per criterion, as `design.md` §8 emits it. It is an
    # array and not a joined string because §8's awaiting_review panel shows
    # acceptance-criteria *results*, plural: a per-criterion pass/fail cannot be
    # recovered from text that was joined on newlines, and the planner would be
    # the one doing the joining. Empty means the node has no stated criteria,
    # which is a different claim from "the criteria are unknown".
    acceptance_criteria: tuple[str, ...] = Field(
        default=(), sa_type=StringTupleType, sa_column_kwargs={"server_default": "[]"}
    )
    # authored — data, never a conditional (invariant 1). No code outside
    # app/harnesses/ may branch on this value. `design.md` §8 calls this
    # `suggested_harness` because the planner only proposes it; once a human has
    # edited the proposal the distinction is gone, and keeping a second column
    # for "what the planner originally said" would be history nobody reads.
    harness: str
    # authored — None means "the harness's own default", which is a different
    # statement from any particular model id.
    model: str | None = Field(default=None)
    # authored — the glob patterns this node is expected to modify
    # (`design.md` §8). `design.md` §12 lists conflicts between parallel nodes
    # as a high-impact risk whose mitigation is "minimize file overlap", and
    # this is the only input any scheduler has for it. Overlap between two globs
    # is a computation over the loaded graph, never a SQL predicate — which is
    # exactly why this is a value on the node while edges are a table.
    touches: tuple[str, ...] = Field(
        default=(),
        sa_type=StringTupleType,
        sa_column_kwargs={"server_default": "[]"},
    )
    # authored — the planner's own size guess ("medium" in `design.md` §8's
    # example). Free text with no CHECK: §8 never closes the vocabulary, and an
    # advisory badge is not worth failing a planner response over. Nothing may
    # schedule on it — an LLM's effort estimate is not a priority.
    estimated_effort: str | None = Field(default=None)
    # derived from the worktree lifecycle, not from the event log: git is the
    # source of truth for these three and orchestrator/worktree.py owns them.
    # They are recorded here so a restart can find the node's diff again.
    worktree_path: Path | None = Field(default=None, sa_type=PathType)
    branch: str | None = Field(default=None)
    base_ref: str | None = Field(default=None)
    # derived — the projection of this node's runs (`docs/architecture.md` §3:
    # one transition() decides it, the scheduler only persists the result).
    status: NodeStatus = Field(
        default=NodeStatus.PENDING,
        sa_column=_status("status", NodeStatus),
    )
    created_ms: int
    updated_ms: int


class NodeDependency(SQLModel, table=True):
    """One edge: ``node_id`` cannot start until ``depends_on_id`` is done.

    Authored. This is `design.md` §5's and §8's ``depends_on``, and no event log
    can invent it — replay rebuilds ``run`` and ``usage_event``, never this.

    **A table and not a JSON column on ``node``.** The scheduler asks "which
    nodes are ready" on every transition; against a blob that is a full scan
    plus a parse, and nothing in a blob can be constrained. Three rules are
    therefore enforced by SQLite itself, because Python is not the only writer —
    the ``sqlite3`` CLI, a maintenance script and a future bulk import all
    bypass :mod:`app.storage.repository`:

    *no self-edge*
        ``ck_node_dependency_no_self_dependency``. A node that depends on itself
        is a cycle of length one and can never become ready.

    *no duplicate edge*
        the composite primary key ``(node_id, depends_on_id)``. A repeated edge
        changes no dependency and silently doubles any count taken over them.

    *both endpoints in the same session*
        the two composite foreign keys onto ``node (id, session_id)``. Both use
        the *same* ``session_id`` column of this row, so the two nodes cannot
        disagree about which session they are in. Without it a session's
        scheduler could wait forever on a node it does not own — and no amount
        of DAG validation *within* a session would ever see the edge.

    All three depend on ``PRAGMA foreign_keys=ON``, which
    :func:`app.storage.db.install_pragmas` sets on every application connection.

    What is deliberately **not** enforced here is cycles of length greater than
    one. A recursive CTE inside a trigger could do it, but the planner needs a
    typed error it can hand back to the model (`design.md` §8), not an
    ``IntegrityError`` raised halfway through inserting a proposal — and the
    check is free to write and exhaustive to test in ``orchestrator/graph.py``,
    which is pure. "Orphan ``depends_on``", the other half of §8's validation,
    cannot exist at this level at all: an endpoint that is not a row is refused
    by the foreign keys, so orphans are only ever a property of the planner's
    JSON, before any of it is persisted.

    ``session_id`` is denormalized from ``node`` and earns it twice: it is what
    makes the composite foreign keys able to compare the two endpoints, and it
    turns "load the whole graph" into one ``WHERE session_id = ?`` instead of a
    join against the node set.
    """

    __tablename__ = "node_dependency"
    __table_args__ = (
        sa.ForeignKeyConstraint(
            ["node_id", "session_id"],
            ["node.id", "node.session_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["depends_on_id", "session_id"],
            ["node.id", "node.session_id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("node_id <> depends_on_id", name="no_self_dependency"),
        # The primary key already indexes (node_id, depends_on_id), which serves
        # "what does this node wait for". The reverse question — "who was
        # waiting on the node that just finished" — is the one the scheduler
        # asks on every completion, and it needs its own index.
        sa.Index("ix_node_dependency_depends_on_id", "depends_on_id"),
    )

    node_id: NodeId = Field(sa_type=sa.String, primary_key=True)
    depends_on_id: NodeId = Field(sa_type=sa.String, primary_key=True)
    session_id: SessionId = Field(sa_type=sa.String, index=True)
    # When the edge was authored. There is no updated_ms: an edge is added or
    # removed, never edited.
    created_ms: int


class Run(SQLModel, table=True):
    """One *execution* of a node (`design.md` §5).

    **A retry is a new row, never a mutated one.** ``uq_run_node_id_attempt``
    makes that structural: attempt history is what tells you a node succeeded on
    the third try, and overwriting the first two destroys the only record that
    it was ever hard. B7 depends on this.

    Every column below is derived from ``runs/<id>/events.ndjson``, except the
    link to the node and :attr:`events_path` itself — the row has to exist
    before the log does, because the log is written *into* the directory this
    row names.
    """

    __tablename__ = "run"
    __table_args__ = (
        sa.UniqueConstraint("node_id", "attempt"),
        _status_check("status", RunState, name="run_state"),
    )

    # The run directory is runs/<id>/, so the id locates the log and the log
    # rebuilds the row. A ULID also encodes created_ms.
    id: RunId = Field(sa_type=sa.String, primary_key=True)
    node_id: NodeId = Field(
        sa_type=sa.String, foreign_key="node.id", ondelete="CASCADE", index=True
    )
    # Denormalized parent. Redundant with node.session_id and always equal to it;
    # it exists so ix_usage_session_ts-style dashboard queries never need a join.
    session_id: SessionId = Field(
        sa_type=sa.String, foreign_key="session.id", ondelete="CASCADE", index=True
    )
    # 1-based. Rebuildable as the run's ordinal among the node's runs (ULIDs
    # sort by time), and unique per node so a retry cannot collide.
    attempt: int
    # derived — RunFinished.status, or RUNNING until one arrives. A row left
    # RUNNING by a crash is an orphan for the scheduler to resolve, which is why
    # the state is persisted on every transition rather than only at the end.
    status: RunState = Field(
        default=RunState.RUNNING,
        sa_column=_status("status", RunState),
    )
    # derived — RunStarted.harness / .model / .cwd / .pid.
    harness: str
    model: str | None = Field(default=None)
    cwd: Path | None = Field(default=None, sa_type=PathType)
    pid: int | None = Field(default=None)
    # derived — RunStarted.session_id: the *harness's* own identifier, kept
    # because it is what resumes a thread and what correlates with the CLI's
    # own logs. Ours is `id`.
    harness_session_id: str | None = Field(default=None)
    harness_version: str | None = Field(default=None)
    # Where the source of truth for this run lives. Absolute; see module docstring.
    events_path: Path = Field(sa_type=PathType)
    # derived — RunStarted.ts and RunFinished.ts. Both null until the event
    # arrives; a run that never started has no start time to invent.
    started_ms: int | None = Field(default=None)
    finished_ms: int | None = Field(default=None)
    # derived — RunFinished.exit_code / .summary. exit_code has no source in any
    # harness's JSON (the adapter synthesizes it from Process.wait()), so a run
    # replayed from a recorded stream legitimately has none.
    exit_code: int | None = Field(default=None)
    summary: str | None = Field(default=None)
    # derived — the number of lines in events.ndjson. Cheap idempotency check
    # for replay: a rebuild that reads a different count read a different log.
    event_count: int = Field(default=0)
    # derived — TurnFinished.permission_denials, summed over the run. Non-zero
    # means the agent was refused and the run reports success anyway
    # (see events.py:TurnFinished); nothing may merge such a run.
    permission_denial_count: int = Field(default=0)
    created_ms: int


class UsageEvent(SQLModel, table=True):
    """Token consumption, append-only (`design.md` §4, invariant 3).

    **Never UPDATEd.** A ``BEFORE UPDATE`` trigger in the first migration
    enforces that in the database, not just by convention: if a value is wrong
    the fix is to replay the log, and a row silently corrected in place is a row
    the log can no longer explain. Rows are removed only by ``ON DELETE
    CASCADE`` when the whole run projection is discarded for a rebuild.

    The four fields of invariant 3 are ``input + output + cache_read +
    cache_write``; ``cache_write_5m``/``cache_write_1h`` are a *breakdown of*
    ``cache_write_tokens``, not additions to it, and they are stored because the
    two TTLs price ~1.25x vs ~2.0x — collapsing them is up to a 1.6x cost error.

    :attr:`cost_usd` is computed **at ingest** from the price table in effect at
    that moment and is nullable: a model absent from ``pricing.yaml`` reads as
    *unknown*, never as ``0.0``, all the way to the UI. :attr:`price_table_version`
    records which table produced it, so a number computed under old prices stays
    attributable — and so a replay that silently repriced history is detectable.
    """

    __tablename__ = "usage_event"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "seq"),
        # design.md §4 names this index; docs/conventions.md §4 repeats the name.
        sa.Index("ix_usage_session_ts", "session_id", "ts"),
        _status_check("source", UsageSource, name="usage_source"),
    )

    id: int | None = Field(default=None, primary_key=True)
    run_id: RunId = Field(
        sa_type=sa.String, foreign_key="run.id", ondelete="CASCADE", index=True
    )
    # Nullable per design.md §4: token spend without a node is a real case —
    # the planner's own LLM calls belong to a session, not to an activity.
    node_id: NodeId | None = Field(
        default=None, sa_type=sa.String, foreign_key="node.id", ondelete="CASCADE"
    )
    session_id: SessionId = Field(
        sa_type=sa.String, foreign_key="session.id", ondelete="CASCADE"
    )
    # 0-based ordinal of this Usage event within the run's log. Two usage events
    # in one run can be identical in every other field, so this is what makes a
    # rebuild verifiably the same set of rows instead of a plausible one.
    seq: int
    # Usage.ts — the harness's stamp when it has one, our ingest time otherwise.
    ts: int
    # From the run: Usage itself carries no harness (invariant 1 — it is data).
    harness: str
    # The raw string the harness reported, never normalized here. Claude Code
    # spells one model two ways and pricing.py decides which prices; a parser
    # that normalized would throw away the evidence.
    model: str
    source: UsageSource = Field(
        default=UsageSource.REPORTED,
        sa_column=_status("source", UsageSource),
    )
    input_tokens: int = Field(default=0)
    output_tokens: int = Field(default=0)
    cache_read_tokens: int = Field(default=0)
    cache_write_tokens: int = Field(default=0)
    cache_write_5m_tokens: int = Field(default=0)
    cache_write_1h_tokens: int = Field(default=0)
    price_table_version: int
    cost_usd: float | None = Field(default=None)
