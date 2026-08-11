"""The topological loop: which nodes run, when, and how many at once.

This is the imperative shell over ``orchestrator/graph.py``
(`docs/architecture.md` §3). Every scheduling question — is this node startable,
is the graph finished, is it stuck, and who is responsible for a node that can
never start — is answered by exactly one call to
:func:`~app.orchestrator.graph.evaluate_graph` per tick. There is no second
readiness rule in this file, deliberately: two implementations of "is this node
ready" that disagree is the failure the pure/impure split exists to prevent, and
the only way to be sure there is one is for the shell to contain none.

`design.md` §9's sketch, and the four things building it forced:

**A tick reads the graph once.** The :class:`~app.orchestrator.graph.Dag` and
the status map are built from the same
:meth:`~app.storage.repository.Repository.load_graph` — three statements,
independent of node count — so ``evaluate_graph``'s "the status map must
describe exactly this DAG" precondition is satisfied by construction rather
than by hope. The DAG is rebuilt every tick instead of once: it is microseconds
of pure code, and caching it would be caching the one thing that must not go
stale relative to the statuses it is evaluated against.

**Blocked propagation is persisted before the outcome is read.** When a node
fails, its dependents are still ``pending`` and
:func:`~app.orchestrator.graph.evaluate_graph` reports them under
``blocked_by_upstream`` while the *outcome* is already ``deadlocked`` — nothing
running, nothing ready, and a ``failed`` node is not a human gate. Writing the
propagation first and re-evaluating is what turns that into
``waiting_on_human``, and it is also what makes ``deadlocked`` mean what C2 says
it means: a transition that was not persisted, i.e. a bug in this file. So it is
logged at error level and returned, never swallowed.

**Concurrency is bounded by launches, not by a semaphore.** §9's sketch creates
a task per node and lets a semaphore inside it queue them. That works, but then
"in flight" and "actually running" are different sets and every subsequent
question — how many slots are free, is anything still ours, did we exceed the
limit — has to distinguish them. Launching at most ``max_concurrency`` tasks and
waiting for one to finish before launching the next keeps a single set, and the
bound becomes an invariant of the loop rather than a property of a counter
somewhere else.

**A node that raises is still a node that has to reach a state.** An agent
failing is data; an exception is a bug (`docs/architecture.md` §9), and this is
the edge of an ``asyncio.Task``, which is the one place a bare ``except
Exception`` is allowed — with structured logging and an explicit transition. A
node left ``pending`` after its task raised would be re-selected on the next
tick and raise again, forever.

**C7 changed nothing here, and that is the result rather than an omission.**
The human gate is entirely expressible in the states this loop already reads:
a finished node under ``auto_merge`` off stops at ``awaiting_review``,
:func:`~app.orchestrator.graph.evaluate_graph` does not count that as done, so
its dependents are never in ``ready`` and the outcome is ``waiting_on_human``
(invariant 6). Approving and rejecting are operations on one node — they merge
or open a new attempt — and both leave the graph in a state the next tick reads
normally. A scheduler that had to know what a review *is* would be a scheduler
with a second readiness rule in it.

**C6's two additions.** Per-node budgets and wall-clock timeouts are *not* in
this file: they are enforced inside the service's ingest loop, where the events
that measure them arrive, and they reach the loop as an ordinary
``NodeExecution`` whose run was interrupted and whose node is ``failed``. The
loop cannot tell a budget kill from any other failure, which is the correct
amount for it to know. What is here is the one thing recovery needs from the
scheduler: resolving the ``running`` rows of a previous process *before* the
first tick, so the loop never has to reason about a node it can neither start
nor wait for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.config import Settings
from app.models.ids import NodeId, SessionId
from app.models.status import NodeStatus
from app.orchestrator.graph import (
    BlockedNode,
    Dag,
    GraphEvaluation,
    GraphNode,
    GraphOutcome,
    InvalidDag,
    build_dag,
    evaluate_graph,
)
from app.orchestrator.service import (
    InvalidGraphError,
    InvalidTransitionError,
    NodeExecution,
    NodeRunService,
    OrchestratorError,
    OrphanResolution,
    ResourceNotFoundError,
)
from app.storage.db import Database
from app.storage.repository import Repository

log = structlog.get_logger()


class NodeLifecycle(Protocol):
    """What the scheduler needs from
    :class:`~app.orchestrator.service.NodeRunService`.

    A protocol rather than the concrete class because these calls are the entire
    coupling. It is also the list of transitions the scheduler is allowed to
    cause: it never writes a node row itself, so the session projection and the
    ordered write path stay in one place.

    **C6 added a fourth call, and the list is deliberately closed, so it is
    worth saying why.** C3 expected the budget and the wall clock to arrive as a
    *wrapper* around the three below, and they did not: enforcing them means
    reading the events of a live stream, which happens inside the service, so
    the loop genuinely learns nothing about either and no method was needed for
    them. What did need one is restart recovery. Resolving a ``running`` row
    left by a dead process moves its node out of ``running`` — a transition, and
    therefore something that has to be on this list rather than a database write
    the scheduler performs behind it.
    """

    async def start_node(
        self, node_id: NodeId, *, parents: Sequence[NodeId] = ()
    ) -> NodeExecution: ...

    async def block_node(
        self, node_id: NodeId, *, causes: Sequence[NodeId]
    ) -> bool: ...

    async def release_resolved_blocks(
        self, session_id: SessionId
    ) -> Sequence[NodeId]: ...

    async def fail_node(self, node_id: NodeId, *, reason: str) -> bool: ...

    async def recover_orphans(
        self, session_id: SessionId
    ) -> Sequence[OrphanResolution]: ...

    async def finalize_session(self, session_id: SessionId) -> object: ...


def _protocol_conformance(service: NodeRunService) -> NodeLifecycle:
    """Make mypy prove the real service still satisfies the protocol.

    Nothing calls this. ``mypy`` runs over ``app`` only, and the composition
    that would otherwise check this happens in tests and in C9's wiring — so
    without it, renaming a parameter on the service would break the scheduler
    with no error anywhere until runtime.
    """
    return service


@dataclass(frozen=True, slots=True)
class GraphRunResult:
    """Why the loop stopped, and everything it did on the way.

    ``blocked_causes`` is accumulated as it is computed, not read off the final
    evaluation: once a node is persisted ``blocked`` it is no longer *startable*
    and :func:`~app.orchestrator.graph.evaluate_graph` stops reporting its
    ancestors. The named causes exist exactly once, in the tick that derives
    them — see C3's report on the missing ``blocked_reason`` column.
    """

    session_id: SessionId
    outcome: GraphOutcome
    evaluation: GraphEvaluation
    executions: Mapping[NodeId, NodeExecution]
    blocked_causes: Mapping[NodeId, tuple[NodeId, ...]]
    unowned_running: tuple[NodeId, ...] = ()
    #: ``running`` run rows left by a previous orchestrator, resolved before the
    #: first tick. Non-empty means this process restarted into unfinished work.
    recovered: tuple[OrphanResolution, ...] = ()

    @property
    def is_complete(self) -> bool:
        return self.outcome is GraphOutcome.COMPLETE

    @property
    def succeeded(self) -> bool:
        return self.evaluation.succeeded

    def blocked_by(self, node_id: NodeId) -> tuple[NodeId, ...]:
        return self.blocked_causes.get(node_id, ())


class GraphScheduler:
    """Drive one session's graph to a terminal or gated state."""

    def __init__(
        self,
        *,
        lifecycle: NodeLifecycle,
        database: Database,
        settings: Settings,
    ) -> None:
        self._lifecycle = lifecycle
        self._database = database
        self._max_concurrency = settings.max_concurrency
        self._scheduled: dict[SessionId, asyncio.Task[GraphRunResult]] = {}

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

    def schedule_graph(self, session_id: SessionId) -> bool:
        """Start one supervised graph task without holding an HTTP request.

        ``False`` means this process already owns a live scheduler for the
        session.  The task is retained until completion, and failures are
        logged at this task edge rather than becoming unobserved exceptions.
        """
        active = self._scheduled.get(session_id)
        if active is not None and not active.done():
            return False
        task = asyncio.create_task(
            self._run_approved_graph(session_id), name=f"graph:{session_id}"
        )
        self._scheduled[session_id] = task

        def completed_callback(completed: asyncio.Task[GraphRunResult]) -> None:
            self._graph_finished(session_id, completed)

        task.add_done_callback(completed_callback)
        return True

    async def close(self) -> None:
        """Cancel every process-owned graph task before database shutdown."""
        tasks = tuple(self._scheduled.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._scheduled.clear()

    def _graph_finished(
        self, session_id: SessionId, task: asyncio.Task[GraphRunResult]
    ) -> None:
        if self._scheduled.get(session_id) is task:
            self._scheduled.pop(session_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            log.error(
                "scheduler.background_failed",
                session_id=session_id,
                error_type=type(error).__name__,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _run_approved_graph(self, session_id: SessionId) -> GraphRunResult:
        """The background entry point enforces the human plan gate.

        Direct :meth:`run_graph` remains the low-level scheduler primitive used
        by exhaustive scheduler tests. Every process-owned background launch
        comes through here, so a caller cannot bypass graph approval merely by
        reaching the scheduler object instead of the REST preflight.
        """
        async with self._database.session() as db_session:
            graph = await Repository(db_session).load_graph(session_id)
        if graph is None:
            raise ResourceNotFoundError(f"no such session {session_id}")
        if graph.nodes and all(
            node.status is NodeStatus.PENDING for node in graph.nodes
        ):
            raise InvalidTransitionError(
                f"graph {session_id} is still a proposal; approve it before running"
            )
        result = await self.run_graph(session_id)
        if result.succeeded:
            try:
                await self._lifecycle.finalize_session(session_id)
            except Exception:
                # Cleanup is best-effort and retryable. The result branch is
                # created before any removal, and a cleanup failure must not
                # turn successfully generated code into a failed session.
                log.exception(
                    "scheduler.session_finalization_failed",
                    session_id=session_id,
                )
        return result

    async def run_graph(self, session_id: SessionId) -> GraphRunResult:
        """Run every node the graph permits, ``max_concurrency`` at a time.

        Returns when the graph is complete, when only a human can move it, or
        when it is deadlocked. Never raises for an agent that failed, a merge
        that conflicted or a node whose base could not be built — those are
        states, and they are in the result.
        """
        executions: dict[NodeId, NodeExecution] = {}
        blocked_causes: dict[NodeId, tuple[NodeId, ...]] = {}
        in_flight: dict[NodeId, asyncio.Task[None]] = {}
        refused: set[NodeId] = set()
        unowned: tuple[NodeId, ...] = ()

        # Before the first tick, not during it. A `running` row from a previous
        # process makes its node neither startable nor finishable, so a graph
        # containing one evaluates to `active` with nothing to do — C3's
        # "unowned running", which correctly refuses to spin. Clearing it first
        # means the loop below only ever sees rows this process owns, and the
        # break at the bottom keeps its original meaning: something is running
        # that recovery decided not to touch.
        recovered = tuple(await self._lifecycle.recover_orphans(session_id))
        if recovered:
            log.warning(
                "scheduler.recovered_orphan_runs",
                session_id=session_id,
                runs={orphan.run_id: orphan.node_status.value for orphan in recovered},
            )

        released = tuple(await self._lifecycle.release_resolved_blocks(session_id))
        if released:
            log.info(
                "scheduler.released_resolved_blocks",
                session_id=session_id,
                nodes=list(released),
            )

        # `docs/conventions.md` §2 prefers `asyncio.TaskGroup`, and this is the
        # documented exception to it. A TaskGroup promises two things the
        # scheduler must not do: it cancels every sibling when one child fails —
        # the opposite of "an agent failing is data", which must leave the other
        # branches of the graph running — and it re-raises everything as an
        # `ExceptionGroup`, which would turn `InvalidGraphError` into a 500 at
        # the transport instead of the 409 it is. Children here are written not
        # to raise; what needs structured cancellation is the loop itself, and
        # `finally: _drain(...)` is exactly that.
        try:
            while True:
                dag, statuses = await self._read_graph(session_id)
                evaluation = evaluate_graph(dag, statuses)

                if await self._propagate_blocked(evaluation, blocked_causes):
                    # Statuses changed underneath the snapshot we just took, so
                    # the outcome it carries is already out of date. Re-read
                    # rather than reason about a graph in two states at once.
                    continue

                if not in_flight and evaluation.outcome is not GraphOutcome.ACTIVE:
                    break

                if evaluation.outcome is GraphOutcome.ACTIVE:
                    for node_id in evaluation.ready:
                        if len(in_flight) >= self._max_concurrency:
                            break
                        if node_id in in_flight or node_id in refused:
                            # Launched on this tick or the previous one and not
                            # yet persisted as `running`, or owned by someone
                            # else. Deduplication, not a second readiness rule.
                            continue
                        in_flight[node_id] = asyncio.create_task(
                            self._execute(
                                node_id,
                                parents=dag.dependencies_of(node_id),
                                executions=executions,
                                refused=refused,
                            ),
                            name=f"node:{node_id}",
                        )

                if not in_flight:
                    # Active, nothing startable, and nothing of ours running:
                    # the `running` rows belong to a process that is not this
                    # one. C6 owns adopting or orphaning them; spinning here
                    # would burn a core waiting for a state nothing can leave.
                    unowned = evaluation.running
                    log.error(
                        "scheduler.unowned_running_nodes",
                        session_id=session_id,
                        nodes=list(unowned),
                    )
                    break

                await asyncio.wait(
                    in_flight.values(), return_when=asyncio.FIRST_COMPLETED
                )
                for node_id, task in tuple(in_flight.items()):
                    if task.done():
                        del in_flight[node_id]

            if evaluation.outcome is GraphOutcome.DEADLOCKED:
                # C2 narrowed this to "nothing running, nothing ready, no gate
                # open, not complete", which a valid DAG can only reach when a
                # transition was not persisted. That is a bug in this file, not
                # a state of the graph, so it is reported at error level.
                log.error(
                    "scheduler.deadlocked",
                    session_id=session_id,
                    pending=[
                        node_id
                        for node_id, status in statuses.items()
                        if status in (NodeStatus.PENDING, NodeStatus.READY)
                    ],
                    failed=list(evaluation.failed),
                )
            else:
                log.info(
                    "scheduler.graph_finished",
                    session_id=session_id,
                    outcome=evaluation.outcome.value,
                    ran=len(executions),
                    failed=list(evaluation.failed),
                    blocked=list(evaluation.blocked),
                    awaiting_review=list(evaluation.awaiting_review),
                )
            return GraphRunResult(
                session_id=session_id,
                outcome=evaluation.outcome,
                evaluation=evaluation,
                executions=executions,
                blocked_causes=blocked_causes,
                unowned_running=unowned,
                recovered=recovered,
            )
        finally:
            await self._drain(in_flight)

    @staticmethod
    async def _drain(in_flight: dict[NodeId, asyncio.Task[None]]) -> None:
        """Stop supervising, so stop the agents too.

        Empty on every normal exit — the loop only leaves when nothing is in
        flight. It is not empty when the loop itself failed or was cancelled,
        and then leaving live harness processes behind with nobody reading their
        streams is strictly worse than killing them: cancellation reaches
        ``_drive``, which kills the process group and records the run
        ``interrupted``, which is a fact a restart can act on.
        """
        if not in_flight:
            return
        log.warning("scheduler.draining", nodes=list(in_flight))
        for task in in_flight.values():
            task.cancel()
        await asyncio.gather(*in_flight.values(), return_exceptions=True)
        in_flight.clear()

    async def _execute(
        self,
        node_id: NodeId,
        *,
        parents: Sequence[NodeId],
        executions: dict[NodeId, NodeExecution],
        refused: set[NodeId],
    ) -> None:
        """One node, start to settled state. The task edge; nothing escapes."""
        try:
            executions[node_id] = await self._lifecycle.start_node(
                node_id, parents=parents
            )
        except asyncio.CancelledError:
            raise
        except OrchestratorError:
            # A *refusal*, not a crash: someone else holds this node's slot, or
            # it already has a live run row, or it has been deleted. Forcing it
            # to `failed` here would overwrite the state of a run that is
            # working perfectly well and is not ours. Stand down instead, and
            # stop selecting it — either it shows up as `running` on a later
            # tick, or the loop reports it as unowned and stops.
            log.warning("scheduler.node_refused", node_id=node_id, exc_info=True)
            refused.add(node_id)
        except Exception as error:
            # Not an agent that failed — the adapter reports that as an event
            # and `start_node` returns it. This is git refusing, the database
            # refusing, or a bug. The node still has to leave `pending`, or the
            # next tick selects it again and reproduces the exception forever.
            log.exception("scheduler.node_crashed", node_id=node_id)
            await self._lifecycle.fail_node(node_id, reason=f"{type(error).__name__}")
            executions[node_id] = NodeExecution(
                node_id=node_id, status=NodeStatus.FAILED
            )

    async def _propagate_blocked(
        self,
        evaluation: GraphEvaluation,
        blocked_causes: dict[NodeId, tuple[NodeId, ...]],
    ) -> tuple[BlockedNode, ...]:
        """Persist ``blocked`` for every node an ancestor made unrunnable.

        Done before the outcome is acted on, so the database describes reality
        at every instant the scheduler could die at, and so a failure's reach —
        which C2 computes, naming the responsible ancestors through any number
        of intermediate ``pending`` nodes — is a persisted fact rather than a
        value that lived in one tick's local variable.
        """
        applied: list[BlockedNode] = []
        for blocked in evaluation.blocked_by_upstream:
            if await self._lifecycle.block_node(blocked.id, causes=blocked.causes):
                blocked_causes[blocked.id] = blocked.causes
                applied.append(blocked)
        if applied:
            log.info(
                "scheduler.blocked_by_upstream",
                nodes={node.id: list(node.causes) for node in applied},
            )
        return tuple(applied)

    async def _read_graph(
        self, session_id: SessionId
    ) -> tuple[Dag, dict[NodeId, NodeStatus]]:
        """The DAG and the statuses, from one read, in three statements."""
        async with self._database.session() as db_session:
            graph = await Repository(db_session).load_graph(session_id)
        if graph is None:
            raise ResourceNotFoundError(f"no such session {session_id}")

        depends_on = graph.depends_on()
        dag = build_dag(
            GraphNode(id=node.id, depends_on=tuple(sorted(depends_on[node.id])))
            for node in graph.nodes
        )
        if isinstance(dag, InvalidDag):
            # C1's foreign keys make orphan and cross-session edges impossible
            # and its composite primary key makes duplicates and self edges
            # impossible, so in practice this is a cycle — which no constraint
            # can see and which C8 must catch before persisting a proposal.
            raise InvalidGraphError(dag.errors)
        return dag, {node.id: node.status for node in graph.nodes}


__all__ = [
    "GraphRunResult",
    "GraphScheduler",
    "NodeLifecycle",
]
