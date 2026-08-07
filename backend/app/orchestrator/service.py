"""The imperative shell around **one node's** agent run.

Transports call it; they do not reproduce its decisions. It composes the
permanent pieces proved in Phase 0 and B3: git worktrees, the harness registry,
mandatory ai-jail policy, ordered ingest, parser trust, checkpoint, and guarded
integration.

There is deliberately no harness-name conditional here (invariant 1). The
registry is the sole name-to-adapter dispatch point and every adapter is driven
through :class:`~app.harnesses.base.BaseHarnessAdapter`.

**What C3 changed, and why.** Phase 1 built this class around "one active run
per session": the mutex, the live-process registry, the "is anything running?"
guard and the session-status projection were all keyed by, or derived from, the
session's single node. A graph makes every one of those wrong — two nodes of one
session are *meant* to run at once. The unit of exclusion is therefore the
**node**, which is the thing that genuinely admits one run at a time (a node has
one worktree and one live process), and the session-addressed methods Phase 1's
REST surface calls survive as thin resolvers on top of the node-addressed ones.

The scheduler is not here. This module knows how to run *a* node; deciding
*which* nodes and *when* is ``orchestrator/scheduler.py``, over the pure core in
``orchestrator/graph.py`` (`docs/architecture.md` §8). The seam between them is
:meth:`NodeRunService.start_node`, which materializes a worktree and runs an
agent in it, and reports a node that could not be materialized as data rather
than as an exception.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import (
    AsyncIterator,
    Awaitable,
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from app.config import Settings
from app.harnesses import create_adapter
from app.harnesses.base import BaseHarnessAdapter, RunHandle, RunSpec
from app.harnesses.events import AgentEvent, RunStarted
from app.models.clock import now_ms
from app.models.ids import (
    NodeId,
    RunId,
    SessionId,
    new_node_id,
    new_run_id,
    new_session_id,
)
from app.models.pricing import PriceTable
from app.models.status import NodeStatus, RunState, SessionStatus
from app.models.tables import Node, Run, Session
from app.orchestrator.graph import (
    DagError,
    GraphNode,
    InvalidDag,
    RunBlockReason,
    build_dag,
    evaluate_run,
)
from app.orchestrator.worktree import (
    CommitResult,
    MergeResult,
    MergeStatus,
    SessionWorkspace,
    init_session_workspace,
)
from app.sandbox.aijail import SandboxPolicy, build_launcher, default_policy
from app.storage.db import Database
from app.storage.ingest import Broadcast, ingest_run, no_broadcast
from app.storage.meta import RunMeta, build_meta, meta_path, read_meta
from app.storage.ndjson import events_path, read_events
from app.storage.repository import Repository, UsageTotals

log = structlog.get_logger()

AdapterFactory = Callable[[str], BaseHarnessAdapter]
PolicyFactory = Callable[[], SandboxPolicy]
RunRegistration = Callable[[RunId, SessionId], Awaitable[None]]

# A node the scheduler may hand to a harness. `design.md` §9's correction: a
# node persisted `ready` and not yet launched when the process died must still
# be startable, so `ready` counts alongside `pending`.
_STARTABLE = frozenset({NodeStatus.PENDING, NodeStatus.READY})

_TERMINAL = frozenset({NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.SKIPPED})


async def no_run_registration(run_id: RunId, session_id: SessionId) -> None:
    """Default registration hook for transports without a live broker."""


class OrchestratorError(Exception):
    """The requested lifecycle operation is invalid for persisted state."""


class ResourceNotFoundError(OrchestratorError):
    """A requested session, node, or run does not exist."""


class InvalidTransitionError(OrchestratorError):
    """Persisted state does not permit the requested operation."""


class InvalidGraphError(OrchestratorError):
    """A proposed or persisted graph is not a DAG.

    Carries the typed defects from :func:`~app.orchestrator.graph.build_dag`
    rather than a rendered string, because C8's correction loop hands them back
    to the planner and needs the node ids, not prose.
    """

    def __init__(self, errors: Sequence[DagError]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(error.message for error in self.errors))


def session_status_for_nodes(statuses: Iterable[NodeStatus]) -> SessionStatus:
    """Project a whole graph's node states onto the session badge.

    Phase 1 could ask ``session_status_for_node(the_one_node)``. A graph has to
    fold, and the fold is not a maximum over some ordering of the enum — the
    question the badge answers is "what, if anything, is this session waiting
    for?", and the answers are ranked by how much they demand of the operator:

    1. something is **running** — nothing is being asked of anyone;
    2. everything is terminal — ``done``, or ``failed`` if any node failed. A
       graph that finished with a failure is a failed graph, not a paused one;
    3. an ``awaiting_review`` or ``blocked`` node — ``paused``, a human is the
       blocking resource (invariant 6: with ``auto_merge`` off this is the
       system working, not a stall);
    4. otherwise only ``pending``/``ready`` remain, which is ``planning``.

    On a single-node session this agrees with
    :func:`~app.orchestrator.graph.session_status_for_node` for all eight node
    states, which is what keeps Phase 1's REST contract intact.

    It belongs in ``orchestrator/graph.py`` beside the function it generalizes —
    it is pure, total and I/O-free — and it is here only because C3 may not edit
    that file. Moving it is a rename, and it is flagged in C3's report.
    """
    collected = tuple(statuses)
    if not collected:
        return SessionStatus.PLANNING
    if NodeStatus.RUNNING in collected:
        return SessionStatus.RUNNING
    if all(status in _TERMINAL for status in collected):
        return (
            SessionStatus.FAILED
            if NodeStatus.FAILED in collected
            else SessionStatus.DONE
        )
    if any(
        status in (NodeStatus.AWAITING_REVIEW, NodeStatus.BLOCKED)
        for status in collected
    ):
        return SessionStatus.PAUSED
    return SessionStatus.PLANNING


@dataclass(frozen=True, slots=True)
class CreatedSession:
    session: Session
    node: Node


@dataclass(frozen=True, slots=True)
class PlannedNode:
    """One activity of a proposed graph, keyed by ``name`` rather than by id.

    `design.md` §8's planner emits ``depends_on`` as a list of the *slugs* it
    invented, because it cannot know the ids the database will allocate.
    :meth:`NodeRunService.create_graph` is the one place that resolves them, so
    an unresolvable name is reported as an
    :data:`~app.orchestrator.graph.DagErrorKind.UNKNOWN_DEPENDENCY` naming the
    slug the planner used.
    """

    name: str
    prompt: str
    harness: str
    model: str | None = None
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    touches: tuple[str, ...] = ()
    estimated_effort: str | None = None


@dataclass(frozen=True, slots=True)
class CreatedGraph:
    session: Session
    nodes: tuple[Node, ...]
    ids_by_name: Mapping[str, NodeId]


@dataclass(frozen=True, slots=True)
class RunOutcome:
    session_id: SessionId
    node_id: NodeId
    run_id: RunId
    run_status: RunState
    node_status: NodeStatus
    trusted: bool
    permission_denials: int
    totals: UsageTotals
    commit: CommitResult
    merge: MergeResult | None
    block_reason: RunBlockReason | None = None


@dataclass(frozen=True, slots=True)
class NodePreparation:
    """The result of materializing a node's worktree (invariant 2)."""

    node_id: NodeId
    blocked: bool = False
    conflicts: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeExecution:
    """What happened to one node when the scheduler picked it up.

    ``outcome`` is ``None`` when no agent ever launched: the node's base could
    not be built out of its parents, so it is ``blocked`` before any process
    starts (`docs/phase-2.md` C4). That is data, not an exception — a scheduler
    that raised here would take every other node down with it
    (`docs/architecture.md` §9).
    """

    node_id: NodeId
    status: NodeStatus
    outcome: RunOutcome | None = None
    conflicts: tuple[Path, ...] = ()


@dataclass(frozen=True, slots=True)
class RunSummary:
    run: Run
    totals: UsageTotals
    trusted: bool


@dataclass(slots=True)
class _ActiveRun:
    run_id: RunId
    adapter: BaseHarnessAdapter
    handle: RunHandle | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    completed: asyncio.Event = field(default_factory=asyncio.Event)
    kill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    kill_requested: bool = False
    kill_sent: bool = False


class NodeRunService:
    """Own the one active run allowed per **node**.

    Several nodes of one session may be in flight at once; that is the whole
    point of a graph. What may not happen twice is a run of the same node, and
    the mutex is keyed accordingly.
    """

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        prices: PriceTable,
        adapter_factory: AdapterFactory = create_adapter,
        policy_factory: PolicyFactory = default_policy,
        broadcast: Broadcast = no_broadcast,
        register_run: RunRegistration = no_run_registration,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._prices = prices
        self._adapter_factory = adapter_factory
        self._policy_factory = policy_factory
        self._broadcast = broadcast
        self._register_run = register_run
        # A copy makes the launch conditions stable for the service lifetime and
        # lets tests prove sanitization without mutating the process environment.
        self._environment = dict(os.environ if environment is None else environment)
        self._locks: dict[NodeId, asyncio.Lock] = {}
        self._active: dict[NodeId, _ActiveRun] = {}
        # Serializes the read-fold-write of the session badge. Two nodes of one
        # session finishing together would otherwise both read the pre-write
        # node statuses and the loser would persist a stale projection.
        self._projection_locks: dict[SessionId, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Authoring
    # ------------------------------------------------------------------

    async def create_session(
        self,
        *,
        repo_path: Path,
        prompt: str,
        harness: str,
        model: str | None = None,
        title: str | None = None,
        acceptance_criteria: Sequence[str] = (),
        auto_merge: bool = False,
        base_ref: str = "HEAD",
    ) -> CreatedSession:
        """Create the integration and fixed-node worktrees plus authored rows.

        The Phase 1 shape: exactly one node, materialized eagerly and left
        ``ready``. A graph is :meth:`create_graph`, whose nodes stay ``pending``
        and unmaterialized until the scheduler reaches them, because a node's
        base is the merge of its parents and no parent has run yet.
        """
        adapter = self._adapter_factory(harness)
        if model is not None and model not in adapter.supported_models:
            raise ValueError(
                f"unsupported model {model!r} for {harness!r}; "
                f"expected one of {adapter.supported_models!r}"
            )
        session_id = new_session_id()
        node_id = new_node_id()
        workspace = await init_session_workspace(
            repo_path=repo_path,
            session_id=session_id,
            workspaces_root=self._settings.workspaces_root,
            base_ref=base_ref,
        )
        node_worktree = await workspace.create_node(node_id)

        async with self._database.session() as db_session:
            repository = Repository(db_session)
            session = await repository.create_session(
                session_id=session_id,
                title=title or prompt[:120],
                repo_path=workspace.repo_path,
                workspace_root=workspace.root,
                integration_branch=workspace.integration_branch,
                auto_merge=auto_merge,
                status=SessionStatus.PLANNING,
            )
            node = await repository.create_node(
                node_id=node_id,
                session_id=session_id,
                name="main",
                prompt=prompt,
                acceptance_criteria=acceptance_criteria,
                harness=harness,
                model=model,
                status=NodeStatus.READY,
            )
            node = await repository.attach_worktree(
                node.id,
                worktree_path=node_worktree.path,
                branch=node_worktree.branch,
                base_ref=node_worktree.base_ref,
            )
        return CreatedSession(session=session, node=node)

    async def create_graph(
        self,
        *,
        repo_path: Path,
        nodes: Sequence[PlannedNode],
        title: str | None = None,
        auto_merge: bool = False,
        base_ref: str = "HEAD",
    ) -> CreatedGraph:
        """Persist a whole proposed graph: session, nodes and edges.

        Validated with :func:`~app.orchestrator.graph.build_dag` **before** a
        single row is written. SQLite rejects self edges, duplicates, unknown
        endpoints and cross-session edges (C1), but a *cycle* is a property of
        the whole graph and no constraint can see it — and half a graph on disk
        with the other half rejected is worse than none.

        Nodes are created ``pending`` and without worktrees. Invariant 6: this
        is a proposal, and nothing about persisting it starts anything.
        """
        allocated: dict[str, NodeId] = {}
        for planned in nodes:
            # First occurrence wins, so a repeated name maps twice onto the same
            # id and build_dag reports it as a duplicate node rather than this
            # method inventing a second error vocabulary.
            allocated.setdefault(planned.name, new_node_id())

        proposal = [
            GraphNode(
                id=allocated[planned.name],
                # An unresolvable name is passed through verbatim: build_dag
                # then names the planner's own slug in the error.
                depends_on=tuple(
                    allocated.get(name, name) for name in planned.depends_on
                ),
            )
            for planned in nodes
        ]
        dag = build_dag(proposal)
        if isinstance(dag, InvalidDag):
            raise InvalidGraphError(dag.errors)

        for planned in nodes:
            adapter = self._adapter_factory(planned.harness)
            if planned.model is not None and planned.model not in (
                adapter.supported_models
            ):
                raise ValueError(
                    f"unsupported model {planned.model!r} for {planned.harness!r}; "
                    f"expected one of {adapter.supported_models!r}"
                )

        session_id = new_session_id()
        workspace = await init_session_workspace(
            repo_path=repo_path,
            session_id=session_id,
            workspaces_root=self._settings.workspaces_root,
            base_ref=base_ref,
        )
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            session = await repository.create_session(
                session_id=session_id,
                title=title or (nodes[0].prompt[:120] if nodes else "graph"),
                repo_path=workspace.repo_path,
                workspace_root=workspace.root,
                integration_branch=workspace.integration_branch,
                auto_merge=auto_merge,
                status=SessionStatus.PLANNING,
            )
            created: list[Node] = []
            for planned in nodes:
                created.append(
                    await repository.create_node(
                        node_id=allocated[planned.name],
                        session_id=session_id,
                        name=planned.name,
                        prompt=planned.prompt,
                        harness=planned.harness,
                        model=planned.model,
                        acceptance_criteria=planned.acceptance_criteria,
                        touches=planned.touches,
                        estimated_effort=planned.estimated_effort,
                        status=NodeStatus.PENDING,
                    )
                )
            for planned in nodes:
                await repository.add_dependencies(
                    allocated[planned.name],
                    [allocated[name] for name in planned.depends_on],
                )
        log.info(
            "orchestrator.graph_created",
            session_id=session_id,
            nodes=len(created),
            auto_merge=auto_merge,
        )
        return CreatedGraph(
            session=session,
            nodes=tuple(created),
            ids_by_name=dict(allocated),
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def start_node(
        self, node_id: NodeId, *, parents: Sequence[NodeId] = ()
    ) -> NodeExecution:
        """Materialize the node's worktree, then run its agent in it.

        The scheduler's single entry point. ``parents`` are the node's
        dependencies; the worktree is created off the first and the rest are
        folded in (`design.md` §2.2), and a fold that conflicts returns a
        ``blocked`` :class:`NodeExecution` **without launching an agent**.
        """
        async with self._node_slot(node_id):
            preparation = await self._prepare(node_id, parents=parents)
            if preparation.blocked:
                return NodeExecution(
                    node_id=node_id,
                    status=NodeStatus.BLOCKED,
                    conflicts=preparation.conflicts,
                )
            outcome = await self._run_locked(node_id)
            return NodeExecution(
                node_id=node_id,
                status=outcome.node_status,
                outcome=outcome,
            )

    async def run_node(self, node_id: NodeId) -> RunOutcome:
        """Run an already-materialized node. See :meth:`start_node`."""
        async with self._node_slot(node_id):
            return await self._run_locked(node_id)

    async def block_node(self, node_id: NodeId, *, causes: Sequence[NodeId]) -> bool:
        """Record that an ancestor makes this node unrunnable.

        ``False`` when the node had already left ``pending``/``ready`` — it
        merged, failed, or was blocked on a previous tick — which lets the
        caller tell "I changed the graph" from "nothing to do" without reading
        the row back.

        The responsible ancestors are logged and returned upward but not
        persisted: ``node`` has no reason column, and adding one is a migration
        C3 may not write. See C3's report.
        """
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            if node.status not in _STARTABLE:
                return False
            await self._set_node(repository, node, NodeStatus.BLOCKED)
        log.info(
            "orchestrator.node_blocked_by_upstream",
            node_id=node_id,
            causes=list(causes),
        )
        return True

    async def fail_node(self, node_id: NodeId, *, reason: str) -> bool:
        """Record a failure that has no run of its own to report it.

        The run path already marks its own node ``failed`` from the adapter's
        terminal event. This is for the failures *around* a run — git refusing
        to build a worktree, the database refusing a write — which would
        otherwise leave the node startable and make the scheduler select it
        again on the next tick, forever.
        """
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            if node.status in _TERMINAL:
                return False
            await self._set_node(repository, node, NodeStatus.FAILED)
        log.warning("orchestrator.node_failed", node_id=node_id, reason=reason)
        return True

    async def run(self, session_id: SessionId) -> RunOutcome:
        """Execute the Phase 1 session's only node."""
        node = await self.get_node(session_id)
        return await self.run_node(node.id)

    async def kill_node(self, node_id: NodeId) -> Run:
        """Terminate one node's process tree and wait for its durable outcome."""
        active = self._active.get(node_id)
        if active is None:
            # Preserve the 404/409 distinction even when nothing is active.
            async with self._database.session() as db_session:
                await self._require_node(Repository(db_session), node_id)
            raise InvalidTransitionError(f"node {node_id} has no active run")
        active.kill_requested = True
        await self._kill_active(active)
        await active.completed.wait()
        async with self._database.session() as db_session:
            run = await Repository(db_session).get_run(active.run_id)
            if run is None:  # pragma: no cover - authored before registration
                raise OrchestratorError(f"run {active.run_id} vanished after kill")
            return run

    async def kill(self, session_id: SessionId) -> Run:
        node = await self.get_node(session_id)
        return await self.kill_node(node.id)

    async def retry_node(self, node_id: NodeId) -> RunOutcome:
        """Create a new attempt after a failed or safety-blocked run.

        B7's rule, unchanged by the graph: a retry is a **new** ``Run`` row and
        a new NDJSON directory. The previous attempt is history and is never
        edited.
        """
        async with self._node_slot(node_id):
            async with self._database.session() as db_session:
                repository = Repository(db_session)
                node = await self._require_node(repository, node_id)
                if node.status not in (NodeStatus.FAILED, NodeStatus.BLOCKED):
                    raise InvalidTransitionError(
                        f"node {node.id} is {node.status.value}; "
                        "only failed or blocked nodes can retry"
                    )
                runs = await repository.list_runs(node.id)
                if not runs or not runs[-1].status.terminal:
                    raise InvalidTransitionError(
                        f"node {node.id} has no terminal run to retry"
                    )
                await self._set_node(repository, node, NodeStatus.READY)
            return await self._run_locked(node_id)

    async def retry(self, session_id: SessionId) -> RunOutcome:
        node = await self.get_node(session_id)
        return await self.retry_node(node.id)

    async def approve_node(self, node_id: NodeId) -> MergeResult:
        """Apply the human gate for a safe run left awaiting review."""
        async with self._node_slot(node_id):
            async with self._database.session() as db_session:
                repository = Repository(db_session)
                node, session = await self._node_and_session(repository, node_id)
                if node.status is not NodeStatus.AWAITING_REVIEW:
                    raise InvalidTransitionError(
                        f"node {node.id} is {node.status.value}, not awaiting_review"
                    )
                runs = await repository.list_runs(node.id)
                if not runs:
                    raise InvalidTransitionError(
                        f"node {node.id} has no run to approve"
                    )
                run = runs[-1]
                meta = await read_meta(meta_path(self._settings.runs_root, run.id))
                if (
                    meta.run_id != run.id
                    or meta.node_id != node.id
                    or meta.session_id != session.id
                ):
                    raise InvalidTransitionError(
                        f"metadata identity does not match run {run.id}; refusing merge"
                    )
                disposition = evaluate_run(
                    run.status,
                    trusted=meta.trusted,
                    permission_denials=run.permission_denial_count,
                    changed=True,
                )
                if not disposition.mergeable:
                    raise InvalidTransitionError(
                        f"run {run.id} is not safe to merge: {disposition.reason}"
                    )
                workspace = self._workspace(session)

            merge = await workspace.merge_into_integration(node.id)
            async with self._database.session() as db_session:
                repository = Repository(db_session)
                node = await self._require_node(repository, node_id)
                await self._set_node(
                    repository,
                    node,
                    NodeStatus.BLOCKED if merge.blocked else NodeStatus.DONE,
                )
            return merge

    async def approve(self, session_id: SessionId) -> MergeResult:
        node = await self.get_node(session_id)
        return await self.approve_node(node.id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def _node_slot(self, node_id: NodeId) -> AsyncIterator[None]:
        """Hold the node's exclusive slot, and clear its live-process entry.

        ``locked()`` rather than a blocking ``acquire()``: a caller asking for a
        second run of a node that is already running is making a mistake, and
        queueing it silently would start an agent minutes later against a
        worktree the first run has since changed.
        """
        lock = self._locks.setdefault(node_id, asyncio.Lock())
        if lock.locked():
            raise InvalidTransitionError(f"node {node_id} already has an active run")
        async with lock:
            try:
                yield
            finally:
                self._complete_active(node_id)

    async def _prepare(
        self, node_id: NodeId, *, parents: Sequence[NodeId]
    ) -> NodePreparation:
        """Give the node a worktree, or report why it cannot have one.

        Idempotent: a node that already carries a ``worktree_path`` — Phase 1's
        eagerly created single node, or a retry — keeps it. Only the transition
        to ``ready`` is persisted, and it is persisted *before* the agent
        starts, so an orchestrator that dies here restarts into a node it can
        pick up again rather than one still marked ``pending``.
        """
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node, session = await self._node_and_session(repository, node_id)
            if node.status not in _STARTABLE:
                raise InvalidTransitionError(
                    f"node {node.id} is {node.status.value}; "
                    "only a pending or ready node can be prepared"
                )
            if node.worktree_path is not None:
                if node.status is not NodeStatus.READY:
                    await self._set_node(repository, node, NodeStatus.READY)
                return NodePreparation(node_id=node_id)
            base_parents = _materializable_parents(
                await repository.list_nodes(session.id), parents
            )
            workspace = self._workspace(session)

        # Outside the database session: `git worktree add` plus a fold of the
        # remaining parents is the slowest thing this class does, and holding a
        # pooled connection across it starves the other nodes' ingest.
        created = await workspace.create_node(node_id, parents=base_parents)

        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await repository.attach_worktree(
                node_id,
                worktree_path=created.path,
                branch=created.branch,
                base_ref=created.base_ref,
            )
            await self._set_node(
                repository,
                node,
                NodeStatus.BLOCKED if created.blocked else NodeStatus.READY,
            )
        if created.blocked:
            log.warning(
                "orchestrator.node_base_conflicted",
                node_id=node_id,
                parents=list(base_parents),
                conflicts=[str(path) for path in created.conflicts],
            )
        return NodePreparation(
            node_id=node_id,
            blocked=created.blocked,
            conflicts=created.conflicts,
        )

    async def _run_locked(self, node_id: NodeId) -> RunOutcome:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node, session = await self._node_and_session(repository, node_id)
            if node.status is not NodeStatus.READY:
                raise InvalidTransitionError(
                    f"node {node.id} is {node.status.value}; "
                    "only a ready node can start"
                )
            if any(
                run.status is RunState.RUNNING
                for run in await repository.list_runs(node.id)
            ):
                raise OrchestratorError(f"node {node.id} already has an active run")
            if node.worktree_path is None:
                raise InvalidTransitionError(f"node {node.id} has no worktree")

            run_id = new_run_id()
            # Resolve the adapter and validate its model/argv before persisting a
            # running attempt. Invalid authored input must not leave an orphan
            # that startup later mistakes for a crashed child.
            adapter = self._adapter_factory(node.harness)
            spec = RunSpec(
                run_id=run_id,
                cwd=node.worktree_path,
                prompt=node.prompt,
                model=node.model,
                env=self._environment,
                launcher=tuple(build_launcher(self._policy_factory())),
            )
            argv = tuple(adapter.build_argv(spec))

            await self._set_node(repository, node, NodeStatus.RUNNING)
            run = await repository.create_run(
                run_id=run_id,
                node_id=node.id,
                events_path=events_path(self._settings.runs_root, run_id),
            )
            await self._register_run(run.id, run.session_id)
            active = _ActiveRun(run_id=run.id, adapter=adapter)
            self._active[node_id] = active
            meta = build_meta(
                run_id=run.id,
                session_id=run.session_id,
                node_id=run.node_id,
                attempt=run.attempt,
                price_table_version=self._prices.version,
                harness=run.harness,
                model=run.model,
                cwd=spec.cwd,
                argv=argv,
                env=spec.env,
                created_ms=run.created_ms,
            )

            finalized = await self._drive(
                repository=repository,
                node=node,
                run=run,
                adapter=adapter,
                spec=spec,
                meta=meta,
                active=active,
            )
            projected = await repository.get_run(run.id)
            if projected is None:  # pragma: no cover - ingest just wrote it
                raise OrchestratorError(f"run {run.id} vanished after ingest")

            try:
                workspace = self._workspace(session)
                commit = await workspace.commit(node.id, f"agent: {node.prompt[:60]}")
                disposition = evaluate_run(
                    projected.status,
                    trusted=finalized.trusted,
                    permission_denials=projected.permission_denial_count,
                    changed=commit.committed,
                )
                merge: MergeResult | None = None
                next_status = disposition.node_status
                if disposition.mergeable and session.auto_merge:
                    merge = await workspace.merge_into_integration(node.id)
                    next_status = (
                        NodeStatus.BLOCKED if merge.blocked else NodeStatus.DONE
                    )
            except Exception:
                await self._set_node(repository, node, NodeStatus.FAILED)
                log.exception(
                    "orchestrator.checkpoint_failed",
                    session_id=node.session_id,
                    node_id=node.id,
                    run_id=run.id,
                )
                raise

            await self._set_node(repository, node, next_status)
            totals = await repository.usage_totals(run_id=run.id)
            log.info(
                "orchestrator.run_finished",
                session_id=node.session_id,
                node_id=node.id,
                run_id=run.id,
                status=projected.status.value,
                trusted=finalized.trusted,
                merged=merge is not None and merge.status is MergeStatus.MERGED,
            )
            return RunOutcome(
                session_id=node.session_id,
                node_id=node.id,
                run_id=run.id,
                run_status=projected.status,
                node_status=next_status,
                trusted=finalized.trusted,
                permission_denials=projected.permission_denial_count,
                totals=totals,
                commit=commit,
                merge=merge,
                block_reason=disposition.reason,
            )

    async def _drive(
        self,
        *,
        repository: Repository,
        node: Node,
        run: Run,
        adapter: BaseHarnessAdapter,
        spec: RunSpec,
        meta: RunMeta,
        active: _ActiveRun,
    ) -> RunMeta:
        handle: RunHandle | None = None
        harness_version: str | None = None
        ingest = None
        try:
            async with ingest_run(
                repository=repository,
                runs_root=self._settings.runs_root,
                meta=meta,
                prices=self._prices,
                broadcast=self._broadcast,
            ) as opened:
                ingest = opened
                handle = await adapter.start(spec)
                active.handle = handle
                active.ready.set()
                if active.kill_requested:
                    await self._kill_active(active)
                async for event in adapter.events(handle):
                    if isinstance(event, RunStarted):
                        harness_version = event.harness_version
                    await ingest.ingest(event)
                finalized = await ingest.finalize(
                    at_ms=now_ms(),
                    stats=adapter.stats,
                    harness_version=harness_version,
                )
            if not ingest.projection.finished:
                await repository.mark_run_interrupted(
                    run.id,
                    at_ms=now_ms(),
                    summary="adapter stream ended without run_finished",
                    event_count=ingest.projection.events,
                    permission_denial_count=ingest.projection.permission_denials,
                )
                await self._set_node(repository, node, NodeStatus.FAILED)
                raise OrchestratorError(
                    f"adapter {adapter.name} ended run {run.id} without run_finished"
                )
            return finalized
        except (Exception, asyncio.CancelledError):
            if handle is not None:
                with contextlib.suppress(Exception):
                    await adapter.kill(handle)
            row = await repository.get_run(run.id)
            if row is not None and row.status is RunState.RUNNING:
                await repository.mark_run_interrupted(
                    run.id,
                    at_ms=now_ms(),
                    summary="orchestrator lost the harness stream",
                    event_count=0 if ingest is None else ingest.projection.events,
                    permission_denial_count=(
                        0 if ingest is None else ingest.projection.permission_denials
                    ),
                )
            await self._set_node(repository, node, NodeStatus.FAILED)
            log.exception(
                "orchestrator.run_crashed",
                session_id=node.session_id,
                node_id=node.id,
                run_id=run.id,
            )
            raise
        finally:
            active.ready.set()

    async def _kill_active(self, active: _ActiveRun) -> None:
        async with active.kill_lock:
            await active.ready.wait()
            if active.handle is None or active.kill_sent:
                return
            await active.adapter.kill(active.handle)
            active.kill_sent = True

    def _complete_active(self, node_id: NodeId) -> None:
        active = self._active.pop(node_id, None)
        if active is None:
            return
        active.ready.set()
        active.completed.set()

    async def _set_node(
        self, repository: Repository, node: Node, status: NodeStatus
    ) -> None:
        """Persist one node transition, then re-project the session badge.

        Node first and separately: it is the fact, the session status is a view
        of it, and the view is recomputed from every sibling's persisted state
        rather than inferred from this one node. The projection runs on its own
        connection because the caller's session may hold stale copies of the
        sibling rows — SQLModel's identity map does not refresh a row another
        connection changed, and a graph is precisely the case where it did.
        """
        await repository.set_node_status(node.id, status)
        await self._project_session_status(node.session_id)

    async def _project_session_status(self, session_id: SessionId) -> None:
        lock = self._projection_locks.setdefault(session_id, asyncio.Lock())
        async with lock, self._database.session() as db_session:
            repository = Repository(db_session)
            nodes = await repository.list_nodes(session_id)
            await repository.set_session_status(
                session_id,
                session_status_for_nodes(node.status for node in nodes),
            )

    @staticmethod
    async def _require_node(repository: Repository, node_id: NodeId) -> Node:
        node = await repository.get_node(node_id)
        if node is None:
            raise ResourceNotFoundError(f"no such node {node_id}")
        return node

    async def _node_and_session(
        self, repository: Repository, node_id: NodeId
    ) -> tuple[Node, Session]:
        node = await self._require_node(repository, node_id)
        session = await repository.get_session(node.session_id)
        if session is None:  # pragma: no cover - foreign key guarantees it
            raise ResourceNotFoundError(f"no such session {node.session_id}")
        return node, session

    async def _session_and_node(
        self, repository: Repository, session_id: SessionId
    ) -> tuple[Session, Node]:
        """Resolve the *one* node of a Phase 1 session.

        Every session-addressed read below still goes through this, and on a
        multi-node graph it refuses rather than guessing which node was meant.
        Node addressing for those routes is C9's; until then a graph session is
        readable through the node-addressed methods only.
        """
        session = await repository.get_session(session_id)
        if session is None:
            raise ResourceNotFoundError(f"no such session {session_id}")
        nodes = await repository.list_nodes(session_id)
        if len(nodes) != 1:
            raise InvalidTransitionError(
                f"session {session_id} is addressed by node: it has {len(nodes)} "
                "nodes, and the session-scoped routes assume exactly one"
            )
        return session, nodes[0]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_sessions(self, *, limit: int | None = None) -> tuple[Session, ...]:
        async with self._database.session() as db_session:
            rows = await Repository(db_session).list_sessions(limit=limit)
            return tuple(rows)

    async def get_session(self, session_id: SessionId) -> Session:
        async with self._database.session() as db_session:
            row = await Repository(db_session).get_session(session_id)
            if row is None:
                raise ResourceNotFoundError(f"no such session {session_id}")
            return row

    async def get_node(self, session_id: SessionId) -> Node:
        async with self._database.session() as db_session:
            _, node = await self._session_and_node(Repository(db_session), session_id)
            return node

    async def list_nodes(self, session_id: SessionId) -> tuple[Node, ...]:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self.get_session(session_id)
            return tuple(await repository.list_nodes(session_id))

    async def list_runs(self, session_id: SessionId) -> tuple[Run, ...]:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            _, node = await self._session_and_node(repository, session_id)
            return tuple(await repository.list_runs(node.id))

    async def get_run_summary(self, session_id: SessionId, run_id: RunId) -> RunSummary:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            _, node = await self._session_and_node(repository, session_id)
            run = await repository.get_run(run_id)
            if run is None or run.node_id != node.id:
                raise ResourceNotFoundError(
                    f"no such run {run_id} in session {session_id}"
                )
            meta = await read_meta(meta_path(self._settings.runs_root, run.id))
            return RunSummary(
                run=run,
                totals=await repository.usage_totals(run_id=run.id),
                trusted=meta.trusted,
            )

    async def list_run_events(
        self, session_id: SessionId, run_id: RunId
    ) -> tuple[AgentEvent, ...]:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            _, node = await self._session_and_node(repository, session_id)
            run = await repository.get_run(run_id)
            if run is None or run.node_id != node.id:
                raise ResourceNotFoundError(
                    f"no such run {run_id} in session {session_id}"
                )
            path = run.events_path
        return await asyncio.to_thread(lambda: tuple(read_events(path)))

    async def get_diff(self, session_id: SessionId) -> str:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            session, node = await self._session_and_node(repository, session_id)
            if node.base_ref is None:
                raise InvalidTransitionError(f"node {node.id} has no base ref")
            return await self._workspace(session).diff(node.id, base_ref=node.base_ref)

    @staticmethod
    def _workspace(session: Session) -> SessionWorkspace:
        return SessionWorkspace(
            session_id=session.id,
            repo_path=session.repo_path,
            root=session.workspace_root,
        )


def _materializable_parents(
    nodes: Sequence[Node], parents: Sequence[NodeId]
) -> tuple[NodeId, ...]:
    """The parents whose branch actually exists, in the order given.

    A ``skipped`` parent satisfies its dependents (`design.md` §9) but never
    ran, so it has no branch and ``git worktree add`` off it would die with
    *"invalid reference"*. Dropping it is right rather than merely convenient:
    the branch is the only thing a parent contributes to a child's base, and a
    node that produced no branch contributes nothing. A node whose every parent
    is skipped is created off the integration branch, exactly like a root.
    """
    branches = {node.id: node.branch for node in nodes}
    resolved: list[NodeId] = []
    for parent in parents:
        if parent not in branches:
            raise OrchestratorError(f"parent {parent} is not a node of this session")
        if branches[parent] is not None:
            resolved.append(parent)
    return tuple(resolved)


# `api/session.py` still imports the Phase 1 name and C3 may not edit it.
# Renaming the import is C9's, when the routes become node-addressed.
SingleRunService = NodeRunService


__all__ = [
    "CreatedGraph",
    "CreatedSession",
    "InvalidGraphError",
    "InvalidTransitionError",
    "NodeExecution",
    "NodePreparation",
    "NodeRunService",
    "OrchestratorError",
    "PlannedNode",
    "ResourceNotFoundError",
    "RunOutcome",
    "RunSummary",
    "SingleRunService",
    "session_status_for_nodes",
]
