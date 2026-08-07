"""Persistence operations over the Phase 1 tables.

The whole surface for reading and writing ``session``, ``node``, ``run`` and
``usage_event``. Nothing above this layer writes SQL, and nothing here decides
*when* something should happen — that is the orchestrator's job
(`docs/architecture.md` §8).

Four rules shape the API.

**A retry is a new run.** :meth:`Repository.create_run` allocates the next
``attempt`` for the node and inserts a row; there is no "reset this run and try
again". ``uq_run_node_id_attempt`` backs it up in the schema.

**``usage_event`` is append-only.** :meth:`Repository.append_usage` is the only
way in, and there is deliberately no ``update_usage`` and no ``delete_usage``.
The only removal is :meth:`Repository.delete_run`, which drops a whole run
projection through ``ON DELETE CASCADE`` so B3 can rebuild it from the log.

**Cost is computed here, at ingest, never in a query.** :meth:`append_usage`
takes the :class:`~app.models.pricing.PriceTable` in effect *at that moment* and
stores both the resulting ``cost_usd`` and the table's version. Aggregates
(:meth:`usage_totals`) only ever ``SUM()`` the stored value.

**Timestamps are supplied, not defaulted.** Every mutation takes ``at_ms``.
Live ingest passes :func:`app.models.clock.now_ms`; replay passes the event's
own ``ts``, which is what lets a rebuilt row equal the original one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.harnesses.events import RunFinished, RunStarted, TurnFinished, Usage
from app.models.clock import now_ms
from app.models.ids import NodeId, RunId, SessionId
from app.models.pricing import PriceTable, TokenCounts
from app.models.status import NodeStatus, RunState, SessionStatus, UsageSource
from app.models.tables import Node, Run, Session, UsageEvent
from app.storage.db import Database


class RepositoryError(Exception):
    """A write referred to something that is not there.

    Programmer error, not agent failure (`docs/architecture.md` §9): appending
    usage for a run that was never created means the caller lost track of its
    own state, and that should surface loudly rather than write an orphan row.
    """


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """The result of aggregating :class:`~app.models.tables.UsageEvent` rows.

    ``cost_usd`` is ``None`` when nothing priced, and ``unpriced_events``
    reports how many rows contributed tokens but no cost. SQL ``SUM()`` skips
    NULLs, so without that count a partially-priced session would present a
    confident total that quietly omits some of its spend.
    """

    counts: TokenCounts
    cost_usd: float | None
    events: int
    unpriced_events: int

    @property
    def complete(self) -> bool:
        """True when every counted event had a known price."""
        return self.unpriced_events == 0


class Repository:
    """Operations on one :class:`~sqlmodel.ext.asyncio.session.AsyncSession`.

    Each mutating method commits: the write order of `docs/architecture.md` §4
    is NDJSON → SQLite → WebSocket, and "SQLite" means durable, not pending in
    a transaction some later event may roll back.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def _persist(self, row: Session | Node | Run | UsageEvent) -> None:
        self._session.add(row)
        await self._session.commit()

    async def create_session(
        self,
        *,
        session_id: SessionId,
        title: str,
        repo_path: Path,
        workspace_root: Path,
        integration_branch: str,
        auto_merge: bool = False,
        status: SessionStatus = SessionStatus.PLANNING,
        at_ms: int | None = None,
    ) -> Session:
        stamp = now_ms() if at_ms is None else at_ms
        row = Session(
            id=session_id,
            title=title,
            repo_path=repo_path,
            workspace_root=workspace_root,
            integration_branch=integration_branch,
            auto_merge=auto_merge,
            status=status,
            created_ms=stamp,
            updated_ms=stamp,
        )
        await self._persist(row)
        return row

    async def get_session(self, session_id: SessionId) -> Session | None:
        return await self._session.get(Session, session_id)

    async def list_sessions(self, *, limit: int | None = None) -> Sequence[Session]:
        """Newest first. ULIDs sort by creation time, so ``id`` is the order."""
        statement = select(Session).order_by(col(Session.id).desc())
        if limit is not None:
            statement = statement.limit(limit)
        return (await self._session.exec(statement)).all()

    async def set_session_status(
        self, session_id: SessionId, status: SessionStatus, *, at_ms: int | None = None
    ) -> Session:
        row = await self._require_session(session_id)
        row.status = status
        row.updated_ms = now_ms() if at_ms is None else at_ms
        await self._persist(row)
        return row

    async def create_node(
        self,
        *,
        node_id: NodeId,
        session_id: SessionId,
        name: str,
        prompt: str,
        harness: str,
        model: str | None = None,
        acceptance_criteria: str | None = None,
        status: NodeStatus = NodeStatus.PENDING,
        at_ms: int | None = None,
    ) -> Node:
        stamp = now_ms() if at_ms is None else at_ms
        row = Node(
            id=node_id,
            session_id=session_id,
            name=name,
            prompt=prompt,
            harness=harness,
            model=model,
            acceptance_criteria=acceptance_criteria,
            status=status,
            created_ms=stamp,
            updated_ms=stamp,
        )
        await self._persist(row)
        return row

    async def get_node(self, node_id: NodeId) -> Node | None:
        return await self._session.get(Node, node_id)

    async def list_nodes(self, session_id: SessionId) -> Sequence[Node]:
        statement = (
            select(Node)
            .where(col(Node.session_id) == session_id)
            .order_by(col(Node.id))
        )
        return (await self._session.exec(statement)).all()

    async def set_node_status(
        self, node_id: NodeId, status: NodeStatus, *, at_ms: int | None = None
    ) -> Node:
        """Persist a transition the orchestrator already decided.

        This method does not evaluate whether the transition is legal — that is
        ``orchestrator/graph.py:transition``'s single responsibility, and
        duplicating the rule here is how the two start disagreeing.
        """
        row = await self._require_node(node_id)
        row.status = status
        row.updated_ms = now_ms() if at_ms is None else at_ms
        await self._persist(row)
        return row

    async def attach_worktree(
        self,
        node_id: NodeId,
        *,
        worktree_path: Path,
        branch: str,
        base_ref: str,
        at_ms: int | None = None,
    ) -> Node:
        """Record where ``orchestrator/worktree.py`` put this node (invariant 2)."""
        row = await self._require_node(node_id)
        row.worktree_path = worktree_path
        row.branch = branch
        row.base_ref = base_ref
        row.updated_ms = now_ms() if at_ms is None else at_ms
        await self._persist(row)
        return row

    async def next_attempt(self, node_id: NodeId) -> int:
        """1 for a node's first run, ``n+1`` after that. Never reuses a number."""
        statement = select(func.max(col(Run.attempt))).where(
            col(Run.node_id) == node_id
        )
        highest = (await self._session.exec(statement)).one()
        return 1 if highest is None else int(highest) + 1

    async def create_run(
        self,
        *,
        run_id: RunId,
        node_id: NodeId,
        events_path: Path,
        harness: str | None = None,
        model: str | None = None,
        attempt: int | None = None,
        at_ms: int | None = None,
    ) -> Run:
        """Open a new attempt at a node.

        A retry calls this again; it never mutates the previous row
        (`design.md` §5). ``harness`` and ``model`` default to the node's, since
        an attempt normally re-runs the same brief — pass them explicitly to
        retry a failed node on a different harness.

        ``attempt`` is allocated automatically. Pass it only when rebuilding a
        known run from its log, where the number is already history.
        """
        node = await self._require_node(node_id)
        stamp = now_ms() if at_ms is None else at_ms
        row = Run(
            id=run_id,
            node_id=node_id,
            session_id=node.session_id,
            attempt=await self.next_attempt(node_id) if attempt is None else attempt,
            status=RunState.RUNNING,
            harness=node.harness if harness is None else harness,
            model=node.model if model is None else model,
            events_path=events_path,
            created_ms=stamp,
        )
        await self._persist(row)
        return row

    async def get_run(self, run_id: RunId) -> Run | None:
        return await self._session.get(Run, run_id)

    async def list_runs(self, node_id: NodeId) -> Sequence[Run]:
        """Every attempt at this node, oldest first. This is the retry history."""
        statement = (
            select(Run).where(col(Run.node_id) == node_id).order_by(col(Run.attempt))
        )
        return (await self._session.exec(statement)).all()

    async def list_session_runs(self, session_id: SessionId) -> Sequence[Run]:
        statement = (
            select(Run).where(col(Run.session_id) == session_id).order_by(col(Run.id))
        )
        return (await self._session.exec(statement)).all()

    async def start_run(self, run_id: RunId, event: RunStarted) -> Run:
        """Project ``RunStarted``: the process is up.

        Everything written here comes off the event, so a replay of the same log
        writes the same values.
        """
        row = await self._require_run(run_id)
        row.status = RunState.RUNNING
        row.harness = event.harness
        row.model = event.model
        row.cwd = event.cwd
        row.pid = event.pid
        row.harness_session_id = event.session_id
        row.harness_version = event.harness_version
        row.started_ms = event.ts
        await self._persist(row)
        return row

    async def finish_run(
        self,
        run_id: RunId,
        event: RunFinished,
        *,
        event_count: int | None = None,
        permission_denial_count: int | None = None,
    ) -> Run:
        """Project ``RunFinished``: the process exited, and how.

        ``permission_denial_count`` is the number of
        :class:`~app.harnesses.events.PermissionDenial` entries seen across the
        run's ``TurnFinished`` events (:meth:`count_permission_denials` sums a
        sequence of them). It is stored because Claude Code reports a run whose
        every write was refused as ``success`` with exit code 0 — the denial
        count is the only signal in the data that the diff is empty, and B4 must
        refuse to merge on it.
        """
        row = await self._require_run(run_id)
        row.status = RunState(event.status)
        row.exit_code = event.exit_code
        row.summary = event.summary
        row.finished_ms = event.ts
        if event_count is not None:
            row.event_count = event_count
        if permission_denial_count is not None:
            row.permission_denial_count = permission_denial_count
        await self._persist(row)
        return row

    async def mark_run_interrupted(
        self,
        run_id: RunId,
        *,
        at_ms: int | None = None,
        summary: str | None = None,
        event_count: int | None = None,
        permission_denial_count: int | None = None,
    ) -> Run:
        """Terminate a run with no ``RunFinished`` in its log.

        The two cases are a kill that never got to write a terminal event, and
        an orphan found at startup — a row still ``running`` whose pid is gone.
        Both are ``interrupted``; neither deserves a sixth run state.
        """
        row = await self._require_run(run_id)
        row.status = RunState.INTERRUPTED
        row.finished_ms = now_ms() if at_ms is None else at_ms
        if summary is not None:
            row.summary = summary
        if event_count is not None:
            row.event_count = event_count
        if permission_denial_count is not None:
            row.permission_denial_count = permission_denial_count
        await self._persist(row)
        return row

    async def list_unfinished_runs(self) -> Sequence[Run]:
        """Rows still ``running``. After a restart these are orphan candidates."""
        statement = select(Run).where(col(Run.status) == RunState.RUNNING)
        return (await self._session.exec(statement)).all()

    async def delete_run(self, run_id: RunId) -> bool:
        """Discard a run projection, cascading to its usage rows.

        The one deletion in this API, and it exists for B3: replay drops the
        derived rows and rebuilds them from ``events.ndjson``. It never touches
        the ``node`` or ``session`` above it — those carry authored input that no
        log can reproduce.

        Callers must capture ``node_id`` before calling: the link from run to
        node is not in the event stream.
        """
        row = await self._session.get(Run, run_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

    async def append_usage(
        self,
        run_id: RunId,
        event: Usage,
        *,
        prices: PriceTable,
        seq: int | None = None,
    ) -> UsageEvent:
        """Insert one usage row, pricing it now (invariant 3).

        ``cost_usd`` is computed against ``prices`` at this instant and stored
        with ``prices.version``. It is ``None`` — never ``0.0`` — when the model
        is absent from the table, because zero is a number someone will trust.

        ``seq`` is the 0-based ordinal of this ``Usage`` within the run's log; it
        defaults to "after the rows already there", which is the same number
        during live ingest and during a rebuild. ``uq_usage_event_run_id_seq``
        turns a double ingest into an error instead of a doubled total.
        """
        run = await self._require_run(run_id)
        counts = TokenCounts(
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            cache_read_tokens=event.cache_read_tokens,
            cache_write_tokens=event.cache_write_tokens,
            cache_write_5m_tokens=event.cache_write_5m_tokens,
            cache_write_1h_tokens=event.cache_write_1h_tokens,
        )
        row = UsageEvent(
            run_id=run_id,
            node_id=run.node_id,
            session_id=run.session_id,
            seq=await self._next_usage_seq(run_id) if seq is None else seq,
            ts=event.ts,
            harness=run.harness,
            model=event.model,
            source=UsageSource(event.source),
            input_tokens=counts.input_tokens,
            output_tokens=counts.output_tokens,
            cache_read_tokens=counts.cache_read_tokens,
            cache_write_tokens=counts.cache_write_tokens,
            cache_write_5m_tokens=counts.cache_write_5m_tokens,
            cache_write_1h_tokens=counts.cache_write_1h_tokens,
            price_table_version=prices.version,
            cost_usd=prices.cost_usd(event.model, counts),
        )
        await self._persist(row)
        return row

    async def list_usage(self, run_id: RunId) -> Sequence[UsageEvent]:
        statement = (
            select(UsageEvent)
            .where(col(UsageEvent.run_id) == run_id)
            .order_by(col(UsageEvent.seq))
        )
        return (await self._session.exec(statement)).all()

    async def usage_totals(
        self,
        *,
        session_id: SessionId | None = None,
        node_id: NodeId | None = None,
        run_id: RunId | None = None,
    ) -> UsageTotals:
        """``SUM()`` over the usage rows, never a stored counter.

        All four token fields (invariant 3): summing only ``input_tokens`` makes
        a long session look ~100x cheaper than it was, because most of the
        volume is cache reads.
        """
        # SUM() over NULL is NULL, so the token fields coalesce to 0 — an empty
        # session has zero tokens. cost_usd deliberately does *not*: no priced
        # row means the cost is unknown, and 0.0 would be a lie (invariant 3).
        # count(cost_usd) skips NULLs, which is what makes the difference below
        # the number of rows we could not price.
        columns: list[Any] = [
            func.coalesce(func.sum(col(UsageEvent.input_tokens)), 0),
            func.coalesce(func.sum(col(UsageEvent.output_tokens)), 0),
            func.coalesce(func.sum(col(UsageEvent.cache_read_tokens)), 0),
            func.coalesce(func.sum(col(UsageEvent.cache_write_tokens)), 0),
            func.coalesce(func.sum(col(UsageEvent.cache_write_5m_tokens)), 0),
            func.coalesce(func.sum(col(UsageEvent.cache_write_1h_tokens)), 0),
            func.sum(col(UsageEvent.cost_usd)),
            func.count(),
            func.count(col(UsageEvent.cost_usd)),
        ]
        statement = select(*columns)
        if session_id is not None:
            statement = statement.where(col(UsageEvent.session_id) == session_id)
        if node_id is not None:
            statement = statement.where(col(UsageEvent.node_id) == node_id)
        if run_id is not None:
            statement = statement.where(col(UsageEvent.run_id) == run_id)

        row = (await self._session.exec(statement)).one()
        events = int(row[7])
        return UsageTotals(
            counts=TokenCounts(
                input_tokens=int(row[0]),
                output_tokens=int(row[1]),
                cache_read_tokens=int(row[2]),
                cache_write_tokens=int(row[3]),
                cache_write_5m_tokens=int(row[4]),
                cache_write_1h_tokens=int(row[5]),
            ),
            cost_usd=None if row[6] is None else float(row[6]),
            events=events,
            unpriced_events=events - int(row[8]),
        )

    async def _next_usage_seq(self, run_id: RunId) -> int:
        statement = select(func.max(col(UsageEvent.seq))).where(
            col(UsageEvent.run_id) == run_id
        )
        highest = (await self._session.exec(statement)).one()
        return 0 if highest is None else int(highest) + 1

    async def _require_session(self, session_id: SessionId) -> Session:
        row = await self._session.get(Session, session_id)
        if row is None:
            raise RepositoryError(f"no such session: {session_id}")
        return row

    async def _require_node(self, node_id: NodeId) -> Node:
        row = await self._session.get(Node, node_id)
        if row is None:
            raise RepositoryError(f"no such node: {node_id}")
        return row

    async def _require_run(self, run_id: RunId) -> Run:
        row = await self._session.get(Run, run_id)
        if row is None:
            raise RepositoryError(f"no such run: {run_id}")
        return row


def count_permission_denials(events: Sequence[TurnFinished]) -> int:
    """Total refusals across a run's turns. See :meth:`Repository.finish_run`."""
    return sum(len(event.permission_denials) for event in events)


@asynccontextmanager
async def repository(database: Database) -> AsyncIterator[Repository]:
    """A :class:`Repository` on a fresh session, closed on the way out."""
    async with database.session() as session:
        yield Repository(session)


__all__ = [
    "Repository",
    "RepositoryError",
    "UsageTotals",
    "count_permission_denials",
    "repository",
]
