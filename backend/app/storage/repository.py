"""Persistence operations over the core tables.

The whole surface for reading and writing ``session``, ``node``,
``node_dependency``, ``run`` and ``usage_event``. Nothing above this layer
writes SQL, and nothing here decides *when* something should happen — that is
the orchestrator's job (`docs/architecture.md` §8).

Six rules shape the API.

**The graph is loaded and stored here, and reasoned about elsewhere.** There is
no cycle detection, no topological sort and no ready set in this module. Those
are total functions over ids with no I/O in them, they belong in
``orchestrator/graph.py`` (`docs/architecture.md` §3), and a second copy living
next to the SQL is how the two start disagreeing. :class:`SessionGraph` is a
read model: rows, and the two adjacency views of the same edge set.

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

**A reviewer's verdict is authored, so replay must never reach it.**
:class:`~app.models.tables.AcceptanceResult` and
:class:`~app.models.tables.NodeReview` are keyed by ``(node_id, attempt)`` and
hang off ``node``, not off ``run`` — :meth:`Repository.delete_run` is what
replay uses to discard a projection, and a foreign key onto ``run`` would put a
human's decision inside the thing invariant 4 says is derived and throwaway.

**Timestamps are supplied, not defaulted.** Every mutation takes ``at_ms``.
Live ingest passes :func:`app.models.clock.now_ms`; replay passes the event's
own ``ts``, which is what lets a rebuilt row equal the original one.
"""

from __future__ import annotations

from collections.abc import (
    AsyncIterator,
    Callable,
    Collection,
    Mapping,
    Sequence,
)
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
from app.models.tables import (
    AcceptanceResult,
    CriterionOutcome,
    Node,
    NodeDependency,
    NodeReview,
    NodeTransition,
    ReviewDecision,
    Run,
    Session,
    UsageEvent,
)
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


@dataclass(frozen=True, slots=True)
class SessionGraph:
    """One session's whole graph, read in a bounded number of queries.

    The scheduler asks for this on **every** transition, so
    :meth:`Repository.load_graph` costs three statements — the session, its
    nodes, its edges — no matter how many nodes there are. One query per node to
    fetch its dependencies would make the cost of a single transition grow with
    the size of the graph, and the scheduler does a transition per node event.

    Deliberately inert. It holds rows and rearranges them; it decides nothing.
    :meth:`depends_on` and :meth:`dependents` are the same edge set seen from
    both ends, because readiness reads one direction ("is everything I wait for
    done?") and completion reads the other ("who was waiting for me?").

    It is a *storage* type and stays one: ``orchestrator/graph.py`` is pure and
    takes plain ids and sets (`docs/architecture.md` §3), so the scheduler — the
    shell — is what converts. Passing SQLModel rows into the pure core would
    drag a database session into the one module that must never need one.
    """

    session: Session
    nodes: tuple[Node, ...]
    edges: tuple[NodeDependency, ...]

    @property
    def node_ids(self) -> tuple[NodeId, ...]:
        """Creation order: ids are ULIDs (`docs/conventions.md` §2)."""
        return tuple(node.id for node in self.nodes)

    def by_id(self) -> dict[NodeId, Node]:
        return {node.id: node for node in self.nodes}

    def depends_on(self) -> dict[NodeId, frozenset[NodeId]]:
        """``node -> what it waits for``. Total: every node has an entry."""
        return self._adjacency(lambda edge: (edge.node_id, edge.depends_on_id))

    def dependents(self) -> dict[NodeId, frozenset[NodeId]]:
        """``node -> who waits for it``. The transpose of :meth:`depends_on`."""
        return self._adjacency(lambda edge: (edge.depends_on_id, edge.node_id))

    def _adjacency(
        self, orient: Callable[[NodeDependency], tuple[NodeId, NodeId]]
    ) -> dict[NodeId, frozenset[NodeId]]:
        # Seeded with every node so a caller never has to distinguish "no
        # dependencies" from "unknown node" with a .get() and a default.
        collected: dict[NodeId, set[NodeId]] = {node.id: set() for node in self.nodes}
        for edge in self.edges:
            key, value = orient(edge)
            collected[key].add(value)
        return {key: frozenset(value) for key, value in collected.items()}


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

    async def _persist(
        self,
        *rows: Session
        | Node
        | NodeDependency
        | Run
        | UsageEvent
        | AcceptanceResult
        | NodeReview
        | NodeTransition,
    ) -> None:
        self._session.add_all(rows)
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
        acceptance_criteria: Sequence[str] = (),
        touches: Sequence[str] = (),
        estimated_effort: str | None = None,
        status: NodeStatus = NodeStatus.PENDING,
        at_ms: int | None = None,
    ) -> Node:
        """Add one activity to a session's graph.

        Edges are not a parameter: a node exists before the graph around it is
        settled — the planner emits ``depends_on`` by its own slugs and only
        this call turns them into ids — and :meth:`add_dependencies` is the one
        that can be given a whole list at once.
        """
        stamp = now_ms() if at_ms is None else at_ms
        row = Node(
            id=node_id,
            session_id=session_id,
            name=name,
            prompt=prompt,
            harness=harness,
            model=model,
            acceptance_criteria=tuple(acceptance_criteria),
            touches=tuple(touches),
            estimated_effort=estimated_effort,
            status=status,
            created_ms=stamp,
            updated_ms=stamp,
        )
        await self._persist(row)
        return row

    async def get_node(self, node_id: NodeId) -> Node | None:
        return await self._session.get(Node, node_id)

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str,
        prompt: str,
        harness: str,
        model: str | None,
        acceptance_criteria: Sequence[str],
        touches: Sequence[str],
        estimated_effort: str | None,
        at_ms: int | None = None,
    ) -> Node:
        """Replace the authored fields of one proposed activity.

        Whether the proposal is still editable is an orchestration decision;
        this method only persists the complete authored value.  Replacing the
        value in one commit avoids a canvas save exposing a half-old node to a
        concurrent graph read.
        """
        row = await self._require_node(node_id)
        row.name = name
        row.prompt = prompt
        row.harness = harness
        row.model = model
        row.acceptance_criteria = tuple(acceptance_criteria)
        row.touches = tuple(touches)
        row.estimated_effort = estimated_effort
        row.updated_ms = now_ms() if at_ms is None else at_ms
        await self._persist(row)
        return row

    async def list_nodes(self, session_id: SessionId) -> Sequence[Node]:
        statement = (
            select(Node)
            .where(col(Node.session_id) == session_id)
            .order_by(col(Node.id))
        )
        return (await self._session.exec(statement)).all()

    async def list_nodes_by_status(
        self,
        statuses: Collection[NodeStatus],
        *,
        session_id: SessionId | None = None,
    ) -> Sequence[Node]:
        """Nodes in any of ``statuses``, oldest first.

        Without ``session_id`` this spans every session, which is what restart
        recovery wants: a node left ``running`` by a dead orchestrator is an
        orphan regardless of whose graph it is in.
        """
        statement = (
            select(Node)
            .where(col(Node.status).in_(list(statuses)))
            .order_by(col(Node.id))
        )
        if session_id is not None:
            statement = statement.where(col(Node.session_id) == session_id)
        return (await self._session.exec(statement)).all()

    async def delete_node(self, node_id: NodeId) -> bool:
        """Remove a node, its edges in **both** directions, and its runs.

        A human editing a proposal, never replay: replay discards ``run`` and
        ``usage_event`` and rebuilds them from the log, but a node carries
        authored input no log can reproduce (see
        :mod:`app.models.tables`). Deleting one that has already executed throws
        away its attempt history, and refusing that is the orchestrator's call
        — this method stores, it does not decide.

        The edges go with it through ``ON DELETE CASCADE`` on both composite
        foreign keys, so a graph can never keep an edge to a node that is gone.
        """
        row = await self._session.get(Node, node_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.commit()
        return True

    async def set_node_status(
        self, node_id: NodeId, status: NodeStatus, *, at_ms: int | None = None
    ) -> Node:
        """Persist a transition the orchestrator already decided.

        This method does not evaluate whether the transition is legal — that is
        ``orchestrator/graph.py:transition``'s single responsibility, and
        duplicating the rule here is how the two start disagreeing.
        """
        row = await self._require_node(node_id)
        stamp = now_ms() if at_ms is None else at_ms
        if row.status is status:
            # A caller may reassert a projection while recovering. Preserve the
            # old timestamp behavior without inventing an activity-feed event.
            row.updated_ms = stamp
            await self._persist(row)
            return row
        row.status = status
        row.updated_ms = stamp
        transition = NodeTransition(
            session_id=row.session_id,
            node_id=row.id,
            status=status,
            ts=stamp,
        )
        await self._persist(row, transition)
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

    async def add_dependencies(
        self,
        node_id: NodeId,
        depends_on: Sequence[NodeId],
        *,
        at_ms: int | None = None,
    ) -> Sequence[NodeDependency]:
        """Make ``node_id`` wait for every node in ``depends_on``.

        One commit for the whole list, because that is the shape the planner
        emits (`design.md` §8: ``depends_on`` is an array per node) and half of
        a node's edge set is a graph the scheduler would happily start running.

        The edge's ``session_id`` is taken from the node rather than from the
        caller, so there is one answer to which session an edge is in — and the
        composite foreign keys then reject any ``depends_on`` node that does not
        agree with it. Everything illegal here is rejected by SQLite: a self
        edge, a duplicate, an unknown endpoint, or an endpoint in another
        session. A **cycle is not**, deliberately — that is a property of the
        whole graph, the planner needs it as a typed error it can hand back to
        the model (`design.md` §8), and it lives in ``orchestrator/graph.py``.
        """
        node = await self._require_node(node_id)
        if not depends_on:
            return []
        stamp = now_ms() if at_ms is None else at_ms
        rows = [
            NodeDependency(
                node_id=node_id,
                depends_on_id=other,
                session_id=node.session_id,
                created_ms=stamp,
            )
            for other in depends_on
        ]
        # The shape of the graph changed, so the node's mtime moved. Without
        # this a *removed* edge would leave no trace of when it went: the row
        # that carried the timestamp is the thing that was deleted.
        node.updated_ms = stamp
        await self._persist(node, *rows)
        return rows

    async def add_dependency(
        self, node_id: NodeId, depends_on_id: NodeId, *, at_ms: int | None = None
    ) -> NodeDependency:
        """One edge. See :meth:`add_dependencies`."""
        return (await self.add_dependencies(node_id, [depends_on_id], at_ms=at_ms))[0]

    async def remove_dependency(
        self, node_id: NodeId, depends_on_id: NodeId, *, at_ms: int | None = None
    ) -> bool:
        """Drop one edge. ``False`` if it was not there."""
        statement = select(NodeDependency).where(
            col(NodeDependency.node_id) == node_id,
            col(NodeDependency.depends_on_id) == depends_on_id,
        )
        row = (await self._session.exec(statement)).one_or_none()
        if row is None:
            return False
        node = await self._require_node(node_id)
        node.updated_ms = now_ms() if at_ms is None else at_ms
        await self._session.delete(row)
        await self._persist(node)
        return True

    async def list_dependencies(
        self, session_id: SessionId
    ) -> Sequence[NodeDependency]:
        """Every edge of one graph, in one query. See :attr:`NodeDependency`."""
        statement = (
            select(NodeDependency)
            .where(col(NodeDependency.session_id) == session_id)
            .order_by(col(NodeDependency.node_id), col(NodeDependency.depends_on_id))
        )
        return (await self._session.exec(statement)).all()

    async def load_graph(self, session_id: SessionId) -> SessionGraph | None:
        """The session, its nodes and its edges — three statements, always.

        ``None`` when there is no such session, like :meth:`get_session`. An
        empty graph is a real state (a session in ``planning`` whose proposal
        has not arrived yet) and reads as a :class:`SessionGraph` with no nodes.
        """
        session = await self._session.get(Session, session_id)
        if session is None:
            return None
        return SessionGraph(
            session=session,
            nodes=tuple(await self.list_nodes(session_id)),
            edges=tuple(await self.list_dependencies(session_id)),
        )

    # ------------------------------------------------------------------
    # The human gate (C7)
    # ------------------------------------------------------------------

    async def record_acceptance_criteria(
        self,
        *,
        node_id: NodeId,
        attempt: int,
        criteria: Sequence[str],
        at_ms: int | None = None,
    ) -> Sequence[AcceptanceResult]:
        """Snapshot one attempt's criteria, each ``unevaluated``.

        This is `design.md` §9's ``check_acceptance(node)``, and §9 is explicit
        that it does not *evaluate* anything: it records what was claimed, so a
        human can resolve it and so the record survives a later edit of
        ``node.acceptance_criteria``.

        Written once per attempt. A second call for the same ``(node, attempt)``
        raises on the primary key, deliberately — the only way to reach it is to
        finalize the same run twice, which is a bug and not a state
        (`docs/architecture.md` §9). Replacing the rows instead would silently
        discard whatever a reviewer had already written on them.
        """
        await self._require_node(node_id)
        if not criteria:
            return []
        stamp = now_ms() if at_ms is None else at_ms
        rows = [
            AcceptanceResult(
                node_id=node_id,
                attempt=attempt,
                position=position,
                criterion=criterion,
                outcome=CriterionOutcome.UNEVALUATED,
                created_ms=stamp,
                updated_ms=stamp,
            )
            for position, criterion in enumerate(criteria)
        ]
        await self._persist(*rows)
        return rows

    async def resolve_acceptance_results(
        self,
        *,
        node_id: NodeId,
        attempt: int,
        outcomes: Mapping[int, CriterionOutcome],
        at_ms: int | None = None,
    ) -> Sequence[AcceptanceResult]:
        """Apply a reviewer's per-criterion verdicts to one attempt.

        Partial by design: a reviewer who resolved two of three criteria and
        approved anyway leaves the third ``unevaluated``, and that is a fact
        worth keeping rather than a form to complete. Positions not mentioned
        keep whatever they had.

        A position with no row is :class:`RepositoryError` — the caller is
        judging a criterion this attempt never had.
        """
        if not outcomes:
            return []
        stamp = now_ms() if at_ms is None else at_ms
        rows: list[AcceptanceResult] = []
        for position, outcome in sorted(outcomes.items()):
            row = await self._session.get(
                AcceptanceResult, (node_id, attempt, position)
            )
            if row is None:
                raise RepositoryError(
                    f"node {node_id} attempt {attempt} has no acceptance criterion "
                    f"at position {position}"
                )
            row.outcome = outcome
            row.updated_ms = stamp
            rows.append(row)
        await self._persist(*rows)
        return rows

    async def list_acceptance_results(
        self, node_id: NodeId, *, attempt: int | None = None
    ) -> Sequence[AcceptanceResult]:
        """Every criterion this node was judged on, oldest attempt first."""
        statement = (
            select(AcceptanceResult)
            .where(col(AcceptanceResult.node_id) == node_id)
            .order_by(col(AcceptanceResult.attempt), col(AcceptanceResult.position))
        )
        if attempt is not None:
            statement = statement.where(col(AcceptanceResult.attempt) == attempt)
        return (await self._session.exec(statement)).all()

    async def record_review(
        self,
        *,
        node_id: NodeId,
        attempt: int,
        decision: ReviewDecision,
        feedback: str | None = None,
        at_ms: int | None = None,
    ) -> NodeReview:
        """Persist the human gate's verdict on one attempt (invariant 6).

        Upserts rather than inserting. One review per attempt is what the
        orchestrator's transitions already enforce — ``approve_node`` demands
        ``awaiting_review`` and ``reject_node`` leaves it — so reaching this
        twice means a reviewer deliberately re-reviewed the same attempt, and
        their newer answer is the one that counts. It does **not** affect the
        accumulation across attempts: those are different rows.
        """
        await self._require_node(node_id)
        stamp = now_ms() if at_ms is None else at_ms
        row = await self._session.get(NodeReview, (node_id, attempt))
        if row is None:
            row = NodeReview(
                node_id=node_id,
                attempt=attempt,
                decision=decision,
                feedback=feedback,
                reviewed_ms=stamp,
            )
        else:
            row.decision = decision
            row.feedback = feedback
            row.reviewed_ms = stamp
        await self._persist(row)
        return row

    async def list_reviews(self, node_id: NodeId) -> Sequence[NodeReview]:
        """This node's review history, oldest attempt first.

        The order is load-bearing: it is the order the rejections are replayed
        into the next attempt's prompt
        (:meth:`app.orchestrator.service.NodeRunService.retry_node`).
        """
        statement = (
            select(NodeReview)
            .where(col(NodeReview.node_id) == node_id)
            .order_by(col(NodeReview.attempt))
        )
        return (await self._session.exec(statement)).all()

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


# The four gate names are re-exported, not redefined: a caller that reads
# review rows through this module should not have to import the table module
# beside it to name what it just read. The vocabulary lives in
# `app/models/tables.py` and only there.
__all__ = [
    "AcceptanceResult",
    "CriterionOutcome",
    "NodeReview",
    "Repository",
    "RepositoryError",
    "ReviewDecision",
    "SessionGraph",
    "UsageTotals",
    "count_permission_denials",
    "repository",
]
