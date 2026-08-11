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

**What C6 added, and why here.** The per-node token budget and wall clock live
in this module rather than in a wrapper around the scheduler's
``NodeLifecycle``, for two reasons. The budget has to be evaluated *between two
events of a live stream*, and only this class is in that loop — a wrapper would
have to poll ``usage_totals`` out of SQLite, which is both slower and later.
And a limit that only the scheduler applied would not protect a node started
through the Phase 1 REST path (:meth:`NodeRunService.run_node`,
:meth:`NodeRunService.retry_node`), which is the same agent burning the same
quota. Restart recovery is here for the matching reason: it writes run rows and
node rows, and those writes and the session projection have exactly one home.

**What C7 added, and why here.** The human gate is three things that all live
next to the run lifecycle because they all read or write it: the criteria
snapshot taken when a run finishes (`design.md` §9's ``check_acceptance``, which
records rather than evaluates), the two verdicts a reviewer can give
(:meth:`NodeRunService.approve_node` and :meth:`NodeRunService.reject_node`), and
the composition of the next attempt's prompt from the rejections that came
before it (:meth:`NodeRunService._compose_prompt`). A rejection is not a separate
mechanism from a retry — it prepares a retry with a reason attached — so it
goes through :meth:`NodeRunService._prepare_retry_locked` with everything else.
Only the scheduler opens the new attempt after an HTTP rejection.

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
import signal
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
from enum import StrEnum
from pathlib import Path

import structlog

from app.config import Settings
from app.harnesses import create_adapter
from app.harnesses.base import BaseHarnessAdapter, RunHandle, RunSpec
from app.harnesses.events import AgentEvent, RunStarted, Usage
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
    Dag,
    DagError,
    GraphNode,
    InvalidDag,
    RunBlockReason,
    build_dag,
    evaluate_graph,
    evaluate_run,
)
from app.orchestrator.worktree import (
    BranchAlreadyExistsError,
    CommitResult,
    FinalizeResult,
    InvalidBranchNameError,
    MergeResult,
    MergeStatus,
    NotARepositoryError,
    SessionWorkspace,
    init_session_workspace,
    validate_final_branch,
    validate_repository,
)
from app.sandbox.aijail import SandboxPolicy, build_launcher, default_policy
from app.storage.db import Database
from app.storage.ingest import Broadcast, ingest_run, no_broadcast
from app.storage.meta import RunMeta, build_meta, meta_path, read_meta
from app.storage.ndjson import events_path, read_events
from app.storage.repository import (
    AcceptanceResult,
    CriterionOutcome,
    NodeReview,
    Repository,
    RepositoryError,
    ReviewDecision,
    SessionGraph,
    UsageTotals,
)

log = structlog.get_logger()

AdapterFactory = Callable[[str], BaseHarnessAdapter]
PolicyFactory = Callable[[], SandboxPolicy]
RunRegistration = Callable[[RunId, SessionId], Awaitable[None]]
NodeTransition = Callable[[Node], Awaitable[None]]

# A node the scheduler may hand to a harness. `design.md` §9's correction: a
# node persisted `ready` and not yet launched when the process died must still
# be startable, so `ready` counts alongside `pending`.
_STARTABLE = frozenset({NodeStatus.PENDING, NodeStatus.READY})

_TERMINAL = frozenset({NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.SKIPPED})

# A node an operator may open another attempt at without reviewing anything.
# `awaiting_review` is deliberately absent: leaving that state is the gate's
# decision (`approve_node`/`reject_node`), and a bare "run it again" there would
# discard a diff a human was in the middle of reading.
#
# A tuple and not a frozenset, for one small reason: it is rendered into the
# refusal message, and a set's iteration order would make that message move
# between runs.
_RETRYABLE = (NodeStatus.FAILED, NodeStatus.BLOCKED)

# What separates the authored prompt from the reviewer's words in a retry. A
# heading rather than a sentence: every harness receives this as ordinary prompt
# text, and prose that could be mistaken for the operator's own instructions is
# how an agent ends up implementing the feedback instead of acting on it.
REVIEW_FEEDBACK_HEADER = (
    "## Reviewer feedback on earlier attempts\n\n"
    "A human reviewed the previous attempts at this same activity and rejected "
    "them for the reasons below. Address all of them; an earlier point is not "
    "superseded by a later one."
)

# How long a previous orchestrator's process gets to honour SIGTERM before this
# one gives up on reaping it. Short: the point is not to wait out a long-running
# agent, it is to distinguish "it took the signal" from "it is ignoring us", and
# the second answer changes the node's state rather than the waiting.
ORPHAN_GRACE_S = 2.0
ORPHAN_POLL_S = 0.1


async def no_run_registration(run_id: RunId, session_id: SessionId) -> None:
    """Default registration hook for transports without a live broker."""


async def no_node_transition(node: Node) -> None:
    """Default transition hook for callers without a graph topic."""


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


async def _open_workspace(
    *,
    repo_path: Path,
    session_id: SessionId,
    workspaces_root: Path | None,
    base_ref: str,
    final_branch: str | None = None,
) -> SessionWorkspace:
    """Create a session's workspace, blaming the request when it is at fault.

    ``worktree.py`` raises one exception family, and only part of it is the
    caller's doing. ``NotARepositoryError`` means the *body* named a path that
    is not a git repository, or a ``base_ref`` with no commit — an argument the
    orchestrator rejects, so it joins the ``ValueError`` → 422 case that already
    covers an unknown harness or an unsupported model.

    The rest of ``WorktreeError`` deliberately keeps propagating as a 500.
    ``GitCommandError`` ("git failed"), ``PathEscapeError``, and
    ``InvalidNameError`` are raised against ids this module generates itself, so
    they are our bugs, not bad input. Translating them too would answer every
    broken repository and every missing ``git`` with a status code that blames
    the operator.
    """
    try:
        return await init_session_workspace(
            repo_path=repo_path,
            session_id=session_id,
            workspaces_root=workspaces_root,
            base_ref=base_ref,
            final_branch=final_branch,
        )
    except (
        BranchAlreadyExistsError,
        InvalidBranchNameError,
        NotARepositoryError,
    ) as exc:
        raise ValueError(str(exc)) from exc


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


class NodeLimit(StrEnum):
    """Which per-node cutoff ended a run (`design.md` §9 and §12).

    Being cut off is **data**, not an exception (`docs/architecture.md` §9): the
    adapter is killed, synthesizes its ordinary
    ``RunFinished(status="interrupted")``, that event is appended, projected and
    broadcast like any other, and the node reaches ``failed`` through the same
    :func:`~app.orchestrator.graph.evaluate_run` every other run goes through.
    Nothing about the cutoff path is special-cased downstream.

    It is deliberately **not persisted**. The run row says ``interrupted``,
    which is what ``events.ndjson`` says and therefore what ``agenthub replay``
    rebuilds (invariant 4); "the orchestrator cut it off, and why" is an
    orchestration fact no harness reports, so its durable home is ``meta.json``
    (`docs/architecture.md` §4) — and adding a field there edits ``storage/``,
    which C6 may not. Until then the reason lives in the log line and in
    :attr:`RunOutcome.limit`, and it is flagged in C6's report.
    """

    TOKEN_BUDGET = "token_budget"
    WALL_CLOCK = "wall_clock"


@dataclass(frozen=True, slots=True)
class NodeLimits:
    """The two cutoffs, resolved once per service rather than read per event.

    ``token_budget`` counts **all four fields** of invariant 3 across every
    :class:`~app.harnesses.events.Usage` event the adapter emits, regardless of
    ``source``. Phase 0's A3 found that a budget-exhausted Claude Code turn
    reports ``result.usage`` as all zeros and B3 reconstructs the real numbers
    from the cumulative ``modelUsage``, marking them ``source="reconstructed"``.
    A check that trusted the harness's own self-reported total, or that filtered
    on ``source``, would read zero in precisely the runaway case. So the budget
    reads the events, and it does not care where they came from.

    Either limit may be ``None``, which disables it. Both being ``None`` removes
    every cutoff, which is a supported configuration and not the default.
    """

    token_budget: int | None = None
    wall_clock_s: float | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> NodeLimits:
        return cls(
            token_budget=settings.node_token_budget,
            wall_clock_s=settings.node_timeout_s,
        )


class ProcessLiveness(StrEnum):
    """What a ``pid`` recorded by a previous orchestrator turned out to be."""

    #: No process with that pid. The run is over whatever the row says.
    GONE = "gone"
    #: A process exists but is provably not the run's — see :func:`probe_process`.
    FOREIGN = "foreign"
    #: A process exists that could be the run's. Could, not is.
    ALIVE = "alive"


ProcessProbe = Callable[[int], ProcessLiveness]
ProcessTerminator = Callable[[int], None]


def probe_process(pid: int) -> ProcessLiveness:
    """Is this pid a plausible survivor of the previous orchestrator?

    Blocking, by design — the caller runs it through
    :func:`asyncio.to_thread` (invariant 5).

    **Pids are reused, so this cannot be an identity check, and it does not
    pretend to be one.** Two things narrow it:

    - ``os.kill(pid, 0)`` sends nothing and only asks whether the pid exists.
      ``PermissionError`` means it exists and belongs to another user, which our
      own child never does — that is ``FOREIGN``.
    - every adapter spawns with ``start_new_session=True``, so a run's process
      is its own session and process-group leader. A pid that is *not* a group
      leader therefore cannot be one of ours, and most recycled pids are not.

    What survives is the narrow case where our process died and the kernel
    handed its pid to a new group leader. That residue is handled by never
    escalating past ``SIGTERM`` to a process group and by
    :meth:`NodeRunService.recover_orphans` treating "alive" as a reason to stop
    rather than as a process to adopt.
    """
    if pid <= 1:
        # 0 and negative are process-group selectors for `kill(2)`, and 1 is
        # launchd. Passing any of them through would signal something enormous.
        return ProcessLiveness.FOREIGN
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return ProcessLiveness.GONE
    except PermissionError:
        return ProcessLiveness.FOREIGN
    try:
        if os.getpgid(pid) != pid:
            return ProcessLiveness.FOREIGN
    except (ProcessLookupError, PermissionError):
        # It exited between the two calls, or it is not ours to inspect.
        return ProcessLiveness.GONE
    return ProcessLiveness.ALIVE


def terminate_process_group(pid: int) -> None:
    """SIGTERM the group ``pid`` leads. Best effort, never raises.

    The group and not the pid: an agent CLI spawns children, and killing only
    the leader leaves them holding the worktree. Callers must have established
    that ``pid`` is the group leader first (:func:`probe_process` does), because
    ``killpg`` on a pgid we did not verify is a much larger blast radius than a
    single misdirected signal.

    No SIGKILL escalation. The adapter escalates because it owns the process and
    knows it is the right one; here we only believe it is, and a stale SIGKILL
    to the wrong group is unrecoverable where a stale SIGTERM usually is not.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, signal.SIGTERM)


@dataclass(frozen=True, slots=True)
class ProcessReaper:
    """Probe and terminate a previous orchestrator's leftover process.

    A value object so a test can drive both halves without spawning anything,
    and so the grace period is a parameter rather than a sleep buried in a
    method.
    """

    probe: ProcessProbe = probe_process
    terminate: ProcessTerminator = terminate_process_group
    grace_s: float = ORPHAN_GRACE_S
    poll_s: float = ORPHAN_POLL_S

    async def reap(self, pid: int) -> tuple[ProcessLiveness, bool]:
        """``(what it was, whether it is gone now)``.

        Both syscalls go through :func:`asyncio.to_thread`: several nodes are in
        flight during a restart sweep and one blocking probe stalls every PTY
        stream at once (invariant 5).
        """
        liveness = await asyncio.to_thread(self.probe, pid)
        if liveness is not ProcessLiveness.ALIVE:
            return liveness, False
        await asyncio.to_thread(self.terminate, pid)
        for _ in range(max(1, round(self.grace_s / self.poll_s))):
            await asyncio.sleep(self.poll_s)
            if await asyncio.to_thread(self.probe, pid) is not ProcessLiveness.ALIVE:
                return liveness, True
        return liveness, False


DEFAULT_REAPER = ProcessReaper()


@dataclass(frozen=True, slots=True)
class OrphanResolution:
    """One ``running`` run row left behind by a process that is not this one."""

    node_id: NodeId
    run_id: RunId
    pid: int | None
    liveness: ProcessLiveness
    #: True when the leftover process was alive and took the SIGTERM.
    terminated: bool
    #: What the node was moved to. ``failed`` when nothing is left holding the
    #: worktree; ``blocked`` when something still is.
    node_status: NodeStatus


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
    #: Set when a per-node cutoff killed this run. ``run_status`` is
    #: ``interrupted`` and ``node_status`` ``failed`` in that case, exactly as
    #: for an operator kill — the cutoff changes why, never how.
    limit: NodeLimit | None = None


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
    # Four-field token total so far, accumulated from the events as they are
    # ingested rather than queried back out of SQLite: the budget has to be
    # checked between two lines of a live stream, and a SELECT per event would
    # put a database round trip in the middle of the PTY path.
    tokens: int = 0
    limit_hit: NodeLimit | None = None


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
        on_transition: NodeTransition = no_node_transition,
        environment: Mapping[str, str] | None = None,
        limits: NodeLimits | None = None,
        reaper: ProcessReaper = DEFAULT_REAPER,
    ) -> None:
        self._database = database
        self._settings = settings
        self._prices = prices
        # Resolved from `Settings` unless a caller overrides them. C7's
        # per-node override, if it happens, replaces this one value.
        self._limits = NodeLimits.from_settings(settings) if limits is None else limits
        self._reaper = reaper
        self._adapter_factory = adapter_factory
        self._policy_factory = policy_factory
        self._broadcast = broadcast
        self._register_run = register_run
        self._on_transition = on_transition
        # A copy makes the launch conditions stable for the service lifetime and
        # lets tests prove sanitization without mutating the process environment.
        self._environment = dict(os.environ if environment is None else environment)
        self._locks: dict[NodeId, asyncio.Lock] = {}
        self._active: dict[NodeId, _ActiveRun] = {}
        # Serializes the read-fold-write of the session badge. Two nodes of one
        # session finishing together would otherwise both read the pre-write
        # node statuses and the loser would persist a stale projection.
        self._projection_locks: dict[SessionId, asyncio.Lock] = {}
        # The final ref is not created until successful finalization, so Git
        # cannot reserve it while two proposals are authored concurrently.
        # AgentHub is a single-process writer; this lock closes that in-process
        # read/check/persist race, including hierarchical names, per repository.
        self._final_branch_locks: dict[Path, asyncio.Lock] = {}

    @property
    def limits(self) -> NodeLimits:
        """The per-node cutoffs in force. Read-only; set at construction."""
        return self._limits

    # ------------------------------------------------------------------
    # Authoring
    # ------------------------------------------------------------------

    async def validate_repo(
        self,
        repo_path: Path,
        *,
        base_ref: str = "HEAD",
        final_branch: str | None = None,
    ) -> Path:
        """Reject an unusable target or occupied final ref before planning."""
        try:
            resolved = (
                await validate_repository(repo_path, base_ref=base_ref)
                if final_branch is None
                else await validate_final_branch(
                    repo_path,
                    final_branch,
                    base_ref=base_ref,
                )
            )
        except (
            BranchAlreadyExistsError,
            InvalidBranchNameError,
            NotARepositoryError,
        ) as exc:
            raise ValueError(str(exc)) from exc
        if final_branch is not None:
            await self._require_unreserved_final_branch(resolved, final_branch)
        return resolved

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
        workspace = await _open_workspace(
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
        final_branch: str | None = None,
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
        requested_final_branch = (
            final_branch
            if final_branch is not None
            else f"agenthub/{session_id}/result"
        )
        resolved_repo = await self.validate_repo(
            repo_path,
            base_ref=base_ref,
            final_branch=requested_final_branch,
        )
        branch_lock = self._final_branch_locks.setdefault(resolved_repo, asyncio.Lock())
        async with branch_lock:
            # Repeat under the authoring lock: planning and model validation can
            # take long enough for another proposal to reserve the same name.
            await self.validate_repo(
                resolved_repo,
                base_ref=base_ref,
                final_branch=requested_final_branch,
            )
            workspace = await _open_workspace(
                repo_path=resolved_repo,
                session_id=session_id,
                workspaces_root=self._settings.workspaces_root,
                base_ref=base_ref,
                final_branch=requested_final_branch,
            )
            async with self._database.session() as db_session:
                repository = Repository(db_session)
                session = await repository.create_session(
                    session_id=session_id,
                    title=title or (nodes[0].prompt[:120] if nodes else "graph"),
                    repo_path=workspace.repo_path,
                    workspace_root=workspace.root,
                    integration_branch=workspace.integration_branch,
                    final_branch=requested_final_branch,
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

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str,
        prompt: str,
        harness: str,
        model: str | None,
        acceptance_criteria: Sequence[str] = (),
        touches: Sequence[str] = (),
        estimated_effort: str | None = None,
    ) -> Node:
        """Replace authored fields while the graph is still a proposal."""
        if not name.strip() or not prompt.strip():
            raise ValueError("node name and prompt must not be blank")
        adapter = self._adapter_factory(harness)
        if model is not None and model not in adapter.supported_models:
            raise ValueError(
                f"unsupported model {model!r} for {harness!r}; "
                f"expected one of {adapter.supported_models!r}"
            )
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            graph = await self._require_editable_graph(repository, node.session_id)
            if any(other.id != node.id and other.name == name for other in graph.nodes):
                raise ValueError(f"node name {name!r} already exists in this graph")
            return await repository.update_node(
                node.id,
                name=name,
                prompt=prompt,
                harness=harness,
                model=model,
                acceptance_criteria=acceptance_criteria,
                touches=touches,
                estimated_effort=estimated_effort,
            )

    async def delete_node(self, node_id: NodeId) -> SessionGraph:
        """Remove one proposed node and both sides of its incident edges."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            graph = await self._require_editable_graph(repository, node.session_id)
            if len(graph.nodes) == 1:
                raise InvalidTransitionError(
                    "a proposed graph must keep at least one node"
                )
            await repository.delete_node(node.id)
            updated = await repository.load_graph(node.session_id)
            if updated is None:  # pragma: no cover - parent FK survived the delete
                raise ResourceNotFoundError(f"no such session {node.session_id}")
            return updated

    async def add_dependency(
        self, node_id: NodeId, depends_on_id: NodeId
    ) -> SessionGraph:
        """Add one proposal edge after validating the resulting whole DAG."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            graph = await self._require_editable_graph(repository, node.session_id)
            by_id = graph.by_id()
            if depends_on_id not in by_id:
                raise ResourceNotFoundError(
                    f"no such dependency {depends_on_id} in session {node.session_id}"
                )
            edge = (node_id, depends_on_id)
            if any((row.node_id, row.depends_on_id) == edge for row in graph.edges):
                raise InvalidTransitionError(
                    f"dependency {node_id} -> {depends_on_id} already exists"
                )
            depends_on = graph.depends_on()
            proposal = [
                GraphNode(
                    id=row.id,
                    depends_on=tuple(
                        sorted(
                            depends_on[row.id]
                            | ({depends_on_id} if row.id == node_id else set())
                        )
                    ),
                )
                for row in graph.nodes
            ]
            dag = build_dag(proposal)
            if isinstance(dag, InvalidDag):
                raise InvalidGraphError(dag.errors)
            await repository.add_dependency(node_id, depends_on_id)
            updated = await repository.load_graph(node.session_id)
            if updated is None:  # pragma: no cover - parent still exists
                raise ResourceNotFoundError(f"no such session {node.session_id}")
            return updated

    async def remove_dependency(
        self, node_id: NodeId, depends_on_id: NodeId
    ) -> SessionGraph:
        """Remove one edge while the graph remains an editable proposal."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            await self._require_editable_graph(repository, node.session_id)
            if not await repository.remove_dependency(node_id, depends_on_id):
                raise ResourceNotFoundError(
                    f"no such dependency {node_id} -> {depends_on_id}"
                )
            updated = await repository.load_graph(node.session_id)
            if updated is None:  # pragma: no cover - parent still exists
                raise ResourceNotFoundError(f"no such session {node.session_id}")
            return updated

    async def approve_graph(self, session_id: SessionId) -> SessionGraph:
        """Approve a proposal by making its root nodes scheduler-ready.

        No approval column is needed: an editable proposal has only ``pending``
        nodes, while an approved graph has at least one root in ``ready``.
        Dependents remain ``pending`` until their parents complete.
        """
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            graph = await self._require_editable_graph(repository, session_id)
            if not graph.nodes:
                raise InvalidTransitionError("an empty graph cannot be approved")
            dag = self._validated_dag(graph)
            roots = [node for node in graph.nodes if not dag.dependencies_of(node.id)]
            for node in roots:
                await self._set_node(repository, node, NodeStatus.READY)
            approved = await repository.load_graph(session_id)
            if approved is None:  # pragma: no cover - session was just loaded
                raise ResourceNotFoundError(f"no such session {session_id}")
            return approved

    async def require_graph_approved(self, session_id: SessionId) -> SessionGraph:
        """Return a graph that has crossed the plan-approval gate."""
        graph = await self.get_graph(session_id)
        self._validated_dag(graph)
        if graph.nodes and all(
            node.status is NodeStatus.PENDING for node in graph.nodes
        ):
            raise InvalidTransitionError(
                f"graph {session_id} is still a proposal; approve it before running"
            )
        return graph

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

    async def release_resolved_blocks(
        self, session_id: SessionId
    ) -> tuple[NodeId, ...]:
        """Return stale upstream-blocked nodes to the schedulable graph.

        A propagated block is structurally identifiable without a new database
        column: the dependent never materialized, so it has no worktree. A
        safety block or a base-merge conflict does have a worktree and must
        remain gated for a human.

        All unmaterialized candidates are evaluated as ``pending`` together so
        :func:`evaluate_graph` remains the only implementation of dependency
        readiness. Candidates that still inherit a failed/blocked ancestor stay
        blocked; the rest return to ``pending`` and can either wait naturally
        or become ready on the scheduler's next tick.
        """
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            graph = await repository.load_graph(session_id)
            if graph is None:
                raise ResourceNotFoundError(f"no such session {session_id}")
            candidates = {
                node.id
                for node in graph.nodes
                if node.status is NodeStatus.BLOCKED and node.worktree_path is None
            }
            if not candidates:
                return ()

            dag = self._validated_dag(graph)
            hypothetical = {
                node.id: (NodeStatus.PENDING if node.id in candidates else node.status)
                for node in graph.nodes
            }
            still_obstructed = {
                blocked.id
                for blocked in evaluate_graph(dag, hypothetical).blocked_by_upstream
            }
            released = tuple(
                node
                for node in graph.nodes
                if node.id in candidates and node.id not in still_obstructed
            )
            for node in released:
                await self._set_node(repository, node, NodeStatus.PENDING)

        if released:
            log.info(
                "orchestrator.upstream_blocks_released",
                session_id=session_id,
                nodes=[node.id for node in released],
            )
        return tuple(node.id for node in released)

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

    async def recover_orphans(
        self, session_id: SessionId | None = None
    ) -> tuple[OrphanResolution, ...]:
        """Resolve every ``running`` run row this process does not own.

        A row still ``RunState.RUNNING`` belongs to a process that is not this
        one, and B2's ``RunState`` docstring already fixed the answer: it
        resolves to ``INTERRUPTED``, not to a sixth state. C3 found the live
        shape of this and named it *unowned running* — ``active`` outcome,
        nothing startable, nothing in flight — and deliberately reported it
        instead of spinning. This is the method that clears it.

        **The run is never adopted, even when its process is still alive.** Not
        because re-finding a pid is racy — it is, and :func:`probe_process` says
        what it can and cannot prove — but because adoption is not implementable
        at all: the adapter reads events off an
        ``asyncio.subprocess.Process``'s pipes, and those pipes died with the
        parent that opened them. A re-found process can emit no further
        ``AgentEvent``, so its log can never reach a terminal event and its run
        can never honestly finish. Adopting it would mean holding a row open
        forever on the strength of a pid.

        So the run is closed and the node is made actionable again. The two
        cases differ only in *how* actionable:

        - the process is gone, foreign, or took the SIGTERM → the node is
          ``failed``, which ``retry_node`` accepts. Nothing holds the worktree.
        - the process is alive and ignored the SIGTERM → the node is
          ``blocked``, and this is logged at error level. ``blocked`` is also
          retryable, but it is not a state the scheduler will drive past on its
          own, which is the point: starting a second agent in a worktree a first
          one is still writing to would corrupt the diff that invariant 2 exists
          to protect.

        Nothing is discarded either way. The worktree, its branch and every
        event already written stay exactly as they are — a partially completed
        node's diff is what a human needs in order to decide.

        Not rebuilt here: ``event_count`` and ``permission_denial_count`` stay
        as live ingest left them. Recomputing them means replaying the log,
        ``agenthub replay`` already does that against the same
        :class:`~app.storage.ingest.Projection`, and a second implementation of
        it here would be the one that drifts.
        """
        async with self._database.session() as db_session:
            unfinished = await Repository(db_session).list_unfinished_runs()

        owned = {active.run_id for active in self._active.values()}
        resolved: list[OrphanResolution] = []
        for run in unfinished:
            if session_id is not None and run.session_id != session_id:
                continue
            if run.id in owned:
                continue
            lock = self._locks.get(run.node_id)
            if lock is not None and lock.locked():
                # A run of this node is being set up or torn down in this very
                # process; its row is legitimately `running` and it is not an
                # orphan. `owned` misses the window between `create_run` and
                # the `_active` assignment, and the slot lock covers it.
                continue
            resolved.append(await self._resolve_orphan(run))
        return tuple(resolved)

    async def _resolve_orphan(self, run: Run) -> OrphanResolution:
        liveness = ProcessLiveness.GONE
        terminated = False
        if run.pid is not None:
            liveness, terminated = await self._reaper.reap(run.pid)
        holding = liveness is ProcessLiveness.ALIVE and not terminated
        node_status = NodeStatus.BLOCKED if holding else NodeStatus.FAILED

        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await repository.mark_run_interrupted(
                run.id,
                at_ms=now_ms(),
                summary="orchestrator restarted; this run had no owning process",
            )
            node = await repository.get_node(run.node_id)
            if node is not None and node.status not in _TERMINAL:
                await self._set_node(repository, node, node_status)

        report = log.error if holding else log.warning
        report(
            "orchestrator.orphan_run_resolved",
            session_id=run.session_id,
            node_id=run.node_id,
            run_id=run.id,
            pid=run.pid,
            liveness=liveness.value,
            terminated=terminated,
            node_status=node_status.value,
        )
        return OrphanResolution(
            node_id=run.node_id,
            run_id=run.id,
            pid=run.pid,
            liveness=liveness,
            terminated=terminated,
            node_status=node_status,
        )

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

    async def retry_node(
        self, node_id: NodeId, *, feedback: str | None = None
    ) -> RunOutcome:
        """Create a new attempt after a failed or safety-blocked run.

        B7's rule, unchanged by the graph: a retry is a **new** ``Run`` row and
        a new NDJSON directory. The previous attempt is history and is never
        edited.

        ``feedback`` is what a human wants the next attempt to do differently.
        It is recorded against the *previous* attempt as a rejection
        (:class:`~app.models.tables.ReviewDecision`) and composed into the new
        run's prompt by :meth:`_compose_prompt`. Passing it here is the
        ``failed``/``blocked`` counterpart of :meth:`reject_node`; both end in
        the same place, which is why there is one path and not two.
        """
        async with self._node_slot(node_id):
            await self._prepare_retry_locked(
                node_id, allowed=_RETRYABLE, feedback=feedback
            )
            return await self._run_locked(node_id)

    async def retry(self, session_id: SessionId) -> RunOutcome:
        node = await self.get_node(session_id)
        return await self.retry_node(node.id)

    async def reject_node(
        self,
        node_id: NodeId,
        *,
        feedback: str,
        outcomes: Mapping[int, CriterionOutcome] | None = None,
    ) -> NodeReview:
        """Reject a reviewed attempt and leave its node ready for scheduling.

        The other half of the human gate (`design.md` §8's ``awaiting_review``
        row, invariant 6). Nothing merges or runs in this call: the rejected
        attempt's commit stays on the node branch, the verdict is made durable,
        and the scheduler later opens the next immutable attempt with the
        reviewer's words appended to the authored prompt.

        ``feedback`` is required and must not be blank. A rejection with no
        reason produces an attempt that differs from the last one only by luck,
        and the reviewer is the only party who knows what was wrong — this is an
        invalid argument, not an agent failure, so it raises
        (`docs/architecture.md` §9).

        ``outcomes`` resolves the acceptance checklist by position, partially if
        that is the truth. It is separate from ``feedback`` because a criterion
        marked ``fail`` says *which* promise was broken and the feedback says
        what to do about it; neither substitutes for the other.
        """
        if not feedback.strip():
            raise ValueError(
                f"rejecting node {node_id} requires feedback: the retry's only "
                "input is what the reviewer says was wrong"
            )
        async with self._node_slot(node_id):
            review = await self._prepare_retry_locked(
                node_id,
                allowed=(NodeStatus.AWAITING_REVIEW,),
                feedback=feedback,
                outcomes=outcomes,
            )
            if review is None:  # pragma: no cover - feedback is required above
                raise OrchestratorError(f"rejection for {node_id} was not recorded")
            return review

    async def approve_node(
        self,
        node_id: NodeId,
        *,
        outcomes: Mapping[int, CriterionOutcome] | None = None,
    ) -> MergeResult:
        """Apply the human gate for a safe run left awaiting review.

        Merges into integration — the one thing invariant 6 says may not happen
        without a human while ``auto_merge`` is off.

        ``outcomes`` is the reviewer's answer to the acceptance checklist. The
        approval is recorded whatever it says, including with a criterion marked
        ``fail``: the reviewer, not this method, decides whether a failed
        criterion is disqualifying, and refusing here would only teach them to
        leave the checklist blank.
        """
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
                # Before the merge, not after. The approval is the human's act
                # and it happened; whether git then managed to fold the branch
                # in is a separate fact, and a conflict must not erase the
                # record of who said yes.
                await self._record_verdict(
                    repository,
                    node_id=node.id,
                    attempt=run.attempt,
                    decision=ReviewDecision.APPROVED,
                    outcomes=outcomes,
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

    async def _prepare_retry_locked(
        self,
        node_id: NodeId,
        *,
        allowed: Sequence[NodeStatus],
        feedback: str | None = None,
        outcomes: Mapping[int, CriterionOutcome] | None = None,
    ) -> NodeReview | None:
        """Record a verdict and make the node ready for a later run.

        The single retry path. :meth:`retry_node` and :meth:`reject_node` differ
        only in which node states they accept and in whether feedback is
        mandatory. The terminal-run check, verdict and transition to ``ready``
        are one sequence; starting the agent is deliberately outside it so an
        HTTP rejection never holds the request for a whole run.

        The verdict is written **before** the transition, so a process that dies
        between them restarts into a ``ready`` node whose accumulated feedback is
        already durable: the scheduler picks it up and the retry carries the
        rejection anyway. The opposite order loses the reviewer's words and
        re-runs the identical prompt, which is the worst of both.
        """
        review: NodeReview | None = None
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node = await self._require_node(repository, node_id)
            if node.status not in allowed:
                expected = " or ".join(status.value for status in allowed)
                raise InvalidTransitionError(
                    f"node {node.id} is {node.status.value}; "
                    f"only {expected} nodes can start a new attempt"
                )
            runs = await repository.list_runs(node.id)
            if not runs or not runs[-1].status.terminal:
                raise InvalidTransitionError(
                    f"node {node.id} has no terminal run to retry"
                )
            if feedback is not None or outcomes:
                review = await self._record_verdict(
                    repository,
                    node_id=node.id,
                    attempt=runs[-1].attempt,
                    decision=ReviewDecision.REJECTED,
                    feedback=feedback,
                    outcomes=outcomes,
                )
            await self._set_node(repository, node, NodeStatus.READY)
        return review

    async def _record_verdict(
        self,
        repository: Repository,
        *,
        node_id: NodeId,
        attempt: int,
        decision: ReviewDecision,
        feedback: str | None = None,
        outcomes: Mapping[int, CriterionOutcome] | None = None,
    ) -> NodeReview:
        """Persist one human decision about one attempt.

        Two rows' worth of authored input, written together: the overall
        verdict and whichever acceptance criteria the reviewer resolved.
        """
        if outcomes:
            await repository.resolve_acceptance_results(
                node_id=node_id, attempt=attempt, outcomes=outcomes
            )
        review = await repository.record_review(
            node_id=node_id,
            attempt=attempt,
            decision=decision,
            feedback=feedback,
        )
        log.info(
            "orchestrator.node_reviewed",
            node_id=node_id,
            attempt=attempt,
            decision=decision.value,
            outcomes={
                position: outcome.value
                for position, outcome in sorted((outcomes or {}).items())
            },
        )
        return review

    async def _compose_prompt(self, repository: Repository, node: Node) -> str:
        """The authored prompt, plus every rejection this node has collected.

        **The node's ``prompt`` is never overwritten.** It is authored input —
        what the operator or the planner actually asked for — and a run is not
        allowed to edit it (``app/models/tables.py``). Feedback is *composed on
        top* at launch, so the row still says what was asked and the transcript
        still says what was asked *this time*.

        **Every earlier rejection, not just the last one.** Attempt 3 sees
        attempt 1's objection and attempt 2's. Dropping the older ones invites
        the classic regression — the agent fixes the newest complaint by undoing
        the fix for the oldest — and would make the reviewer restate every
        surviving objection in every rejection, which is work the record has
        already done. The growth is bounded by the number of attempts, which is
        a human decision each time, and the token budget bounds it again
        (:class:`NodeLimits`).

        Nothing is composed for a node that was never rejected: its prompt is
        byte-for-byte what was authored.
        """
        rejections = [
            review
            for review in await repository.list_reviews(node.id)
            if review.decision is ReviewDecision.REJECTED and review.feedback
        ]
        if not rejections:
            return node.prompt
        parts = [node.prompt, "", REVIEW_FEEDBACK_HEADER]
        for review in rejections:
            parts.append(f"### Attempt {review.attempt}")
            parts.append(str(review.feedback))
        return "\n\n".join(parts)

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
            prepared_status = (
                NodeStatus.BLOCKED if created.blocked else NodeStatus.READY
            )
            if node.status is not prepared_status:
                await self._set_node(repository, node, prepared_status)
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
                # Authored prompt plus every rejection so far, composed here and
                # nowhere else so that a run started by the scheduler, by the
                # Phase 1 REST path or by a retry all carry the same history.
                prompt=await self._compose_prompt(repository, node),
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
                commit = await workspace.commit(
                    node.id,
                    f"agent: {node.prompt[:60]}",
                    base_ref=node.base_ref,
                )
                disposition = evaluate_run(
                    projected.status,
                    trusted=finalized.trusted,
                    permission_denials=projected.permission_denial_count,
                    changed=commit.committed,
                )
                # `design.md` §9's check_acceptance(node), in its place in the
                # sketch — after execute, before the gate — and doing what §9
                # says it does: recording each criterion against an outcome,
                # never evaluating it. Unconditional, including for a run that
                # failed: the value of the snapshot is that it says what was
                # being asked of *this* attempt, and that is as true of an
                # attempt nobody will review as of one somebody will.
                await repository.record_acceptance_criteria(
                    node_id=node.id,
                    attempt=run.attempt,
                    criteria=node.acceptance_criteria,
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
                limit=None if active.limit_hit is None else active.limit_hit.value,
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
                limit=active.limit_hit,
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
                async with self._under_wall_clock(active, run):
                    async for event in adapter.events(handle):
                        if isinstance(event, RunStarted):
                            harness_version = event.harness_version
                        # Durable first, then decide. A budget kill must never
                        # cost the tokens that triggered it: they were really
                        # spent, and dropping the event that reports them is
                        # how a dashboard ends up unable to explain a kill.
                        await ingest.ingest(event)
                        await self._check_token_budget(active, run, event)
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

    @asynccontextmanager
    async def _under_wall_clock(
        self, active: _ActiveRun, run: Run
    ) -> AsyncIterator[None]:
        """Kill the run if it outlives ``node_timeout_s``.

        A background task rather than :func:`asyncio.timeout` around the event
        loop, because a timeout raises and cancels, and being cut off is not an
        exception (`docs/architecture.md` §9). Cancelling here would skip the
        checkpoint commit and leave the partial work uncommitted; killing lets
        the adapter close the stream with its ordinary
        ``RunFinished(status="interrupted")`` and the run finishes down the same
        path as every other run.

        The clock starts here, after ``adapter.start`` returned — i.e. at
        process launch, not at :class:`~app.harnesses.events.RunStarted`. They
        are milliseconds apart when the harness is healthy and unboundedly apart
        when it is not, and the unhealthy case is the one a timeout exists for.
        """
        seconds = self._limits.wall_clock_s
        if seconds is None:
            yield
            return

        async def expire() -> None:
            await asyncio.sleep(seconds)
            if active.limit_hit is None:
                active.limit_hit = NodeLimit.WALL_CLOCK
            log.warning(
                "orchestrator.node_wall_clock_exceeded",
                session_id=run.session_id,
                node_id=run.node_id,
                run_id=run.id,
                seconds=seconds,
            )
            await self._kill_active(active)

        watchdog = asyncio.create_task(expire(), name=f"wall-clock:{run.id}")
        try:
            yield
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog

    async def _check_token_budget(
        self, active: _ActiveRun, run: Run, event: AgentEvent
    ) -> None:
        """Kill the run once its four-field token total passes the budget.

        Counted from the :class:`~app.harnesses.events.Usage` events themselves,
        every one of them, ``reported`` and ``reconstructed`` alike — see
        :class:`NodeLimits`. ``Usage.total_tokens`` is the four fields of
        invariant 3; a check on ``input_tokens`` alone would be short by roughly
        100x and would never fire on the session it exists to stop.
        """
        budget = self._limits.token_budget
        if budget is None or not isinstance(event, Usage):
            return
        active.tokens += event.total_tokens
        if active.tokens <= budget or active.limit_hit is not None:
            return
        active.limit_hit = NodeLimit.TOKEN_BUDGET
        log.warning(
            "orchestrator.node_token_budget_exceeded",
            session_id=run.session_id,
            node_id=run.node_id,
            run_id=run.id,
            tokens=active.tokens,
            budget=budget,
        )
        await self._kill_active(active)

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
        persisted = await repository.set_node_status(node.id, status)
        await self._project_session_status(node.session_id)
        await self._on_transition(persisted)

    async def _require_editable_graph(
        self, repository: Repository, session_id: SessionId
    ) -> SessionGraph:
        """A proposal is editable only before approval changes a node state."""
        graph = await repository.load_graph(session_id)
        if graph is None:
            raise ResourceNotFoundError(f"no such session {session_id}")
        if any(node.status is not NodeStatus.PENDING for node in graph.nodes):
            raise InvalidTransitionError(
                f"graph {session_id} is no longer an editable pending proposal"
            )
        return graph

    @staticmethod
    def _validated_dag(graph: SessionGraph) -> Dag:
        depends_on = graph.depends_on()
        dag = build_dag(
            GraphNode(id=node.id, depends_on=tuple(sorted(depends_on[node.id])))
            for node in graph.nodes
        )
        if isinstance(dag, InvalidDag):
            raise InvalidGraphError(dag.errors)
        return dag

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
            if await repository.get_session(session_id) is None:
                raise ResourceNotFoundError(f"no such session {session_id}")
            return tuple(await repository.list_nodes(session_id))

    async def get_graph(self, session_id: SessionId) -> SessionGraph:
        """Return the session, nodes and edges in one bounded storage read."""
        async with self._database.session() as db_session:
            graph = await Repository(db_session).load_graph(session_id)
            if graph is None:
                raise ResourceNotFoundError(f"no such session {session_id}")
            return graph

    async def get_graph_diff(self, session_id: SessionId) -> str:
        """Return every generated change currently on the integration branch."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            graph = await repository.load_graph(session_id)
            if graph is None:
                raise ResourceNotFoundError(f"no such session {session_id}")
            dependencies = graph.depends_on()
            root_base_refs = tuple(
                node.base_ref
                for node in graph.nodes
                if not dependencies[node.id] and node.base_ref is not None
            )
            if not root_base_refs:
                raise InvalidTransitionError(
                    f"graph {session_id} has no materialized root base ref"
                )
            workspace = self._workspace(graph.session)
        return await workspace.integration_diff(base_refs=root_base_refs)

    async def get_graph_result_branch(self, session_id: SessionId) -> str:
        graph = await self.get_graph(session_id)
        return self._workspace(graph.session).result_branch

    async def finalize_session(self, session_id: SessionId) -> FinalizeResult:
        """Create the durable result branch and clean completed worktrees."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            graph = await repository.load_graph(session_id)
            if graph is None:
                raise ResourceNotFoundError(f"no such session {session_id}")
            if graph.session.status is not SessionStatus.DONE or any(
                node.status not in (NodeStatus.DONE, NodeStatus.SKIPPED)
                for node in graph.nodes
            ):
                raise InvalidTransitionError(
                    f"session {session_id} is not successfully complete"
                )
            workspace = self._workspace(graph.session)
            node_ids = tuple(node.id for node in graph.nodes)
        try:
            return await workspace.finalize(node_ids=node_ids)
        except BranchAlreadyExistsError as exc:
            raise InvalidTransitionError(str(exc)) from exc

    async def acceptance_results(
        self, node_id: NodeId, *, attempt: int | None = None
    ) -> tuple[AcceptanceResult, ...]:
        """The acceptance checklist, per attempt (`design.md` §8's panel).

        Every criterion the node has been judged on, oldest attempt first, with
        the outcome a human gave it — or ``unevaluated``, which is the honest
        answer both before anyone has looked and forever after under
        ``auto_merge``.
        """
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self._require_node(repository, node_id)
            return tuple(
                await repository.list_acceptance_results(node_id, attempt=attempt)
            )

    async def reviews(self, node_id: NodeId) -> tuple[NodeReview, ...]:
        """This node's approvals and rejections, oldest attempt first."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self._require_node(repository, node_id)
            return tuple(await repository.list_reviews(node_id))

    async def resolve_acceptance_results(
        self,
        node_id: NodeId,
        *,
        attempt: int,
        outcomes: Mapping[int, CriterionOutcome],
    ) -> tuple[AcceptanceResult, ...]:
        """Resolve checklist entries without recording an overall verdict."""
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self._require_node(repository, node_id)
            try:
                rows = await repository.resolve_acceptance_results(
                    node_id=node_id, attempt=attempt, outcomes=outcomes
                )
            except RepositoryError as error:
                raise ValueError(str(error)) from error
            return tuple(rows)

    async def list_node_runs(self, node_id: NodeId) -> tuple[Run, ...]:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self._require_node(repository, node_id)
            return tuple(await repository.list_runs(node_id))

    async def list_runs(self, session_id: SessionId) -> tuple[Run, ...]:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            _, node = await self._session_and_node(repository, session_id)
            return tuple(await repository.list_runs(node.id))

    async def get_node_run_summary(self, node_id: NodeId, run_id: RunId) -> RunSummary:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self._require_node(repository, node_id)
            run = await repository.get_run(run_id)
            if run is None or run.node_id != node_id:
                raise ResourceNotFoundError(f"no such run {run_id} for node {node_id}")
            meta = await read_meta(meta_path(self._settings.runs_root, run.id))
            return RunSummary(
                run=run,
                totals=await repository.usage_totals(run_id=run.id),
                trusted=meta.trusted,
            )

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

    async def list_node_run_events(
        self, node_id: NodeId, run_id: RunId
    ) -> tuple[AgentEvent, ...]:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            await self._require_node(repository, node_id)
            run = await repository.get_run(run_id)
            if run is None or run.node_id != node_id:
                raise ResourceNotFoundError(f"no such run {run_id} for node {node_id}")
            path = run.events_path
        return await asyncio.to_thread(lambda: tuple(read_events(path)))

    async def get_diff(self, session_id: SessionId) -> str:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            session, node = await self._session_and_node(repository, session_id)
            if node.base_ref is None:
                raise InvalidTransitionError(f"node {node.id} has no base ref")
            return await self._workspace(session).diff(node.id, base_ref=node.base_ref)

    async def get_node_diff(self, node_id: NodeId) -> str:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            node, session = await self._node_and_session(repository, node_id)
            if node.base_ref is None:
                raise InvalidTransitionError(f"node {node.id} has no base ref")
            return await self._workspace(session).diff(node.id, base_ref=node.base_ref)

    @staticmethod
    def _workspace(session: Session) -> SessionWorkspace:
        return SessionWorkspace(
            session_id=session.id,
            repo_path=session.repo_path,
            root=session.workspace_root,
            final_branch=session.final_branch,
        )

    async def _require_unreserved_final_branch(
        self, repo_path: Path, final_branch: str
    ) -> None:
        async with self._database.session() as db_session:
            active = await Repository(db_session).list_nonfinal_sessions_for_repo(
                repo_path
            )
        for existing in active:
            reserved = existing.final_branch
            if (
                reserved == final_branch
                or reserved.startswith(f"{final_branch}/")
                or final_branch.startswith(f"{reserved}/")
            ):
                raise ValueError(
                    f"final branch {final_branch!r} conflicts with branch "
                    f"{reserved!r} reserved by non-final session {existing.id}"
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


__all__ = [
    "DEFAULT_REAPER",
    "REVIEW_FEEDBACK_HEADER",
    "AcceptanceResult",
    "CreatedGraph",
    "CreatedSession",
    "CriterionOutcome",
    "InvalidGraphError",
    "InvalidTransitionError",
    "NodeExecution",
    "NodeLimit",
    "NodeLimits",
    "NodePreparation",
    "NodeReview",
    "NodeRunService",
    "OrchestratorError",
    "OrphanResolution",
    "PlannedNode",
    "ProcessLiveness",
    "ProcessReaper",
    "ResourceNotFoundError",
    "ReviewDecision",
    "RunOutcome",
    "RunSummary",
    "probe_process",
    "session_status_for_nodes",
    "terminate_process_group",
]
