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

Budgets, wall-clock timeouts and restart recovery are **C6**. The seams they
need are here — every transition goes through the node lifecycle service, and
in-flight tasks are cancellable — and nothing else about them is.
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
    NodeExecution,
    NodeRunService,
    OrchestratorError,
    ResourceNotFoundError,
)
from app.storage.db import Database
from app.storage.repository import Repository

log = structlog.get_logger()


class NodeLifecycle(Protocol):
    """What the scheduler needs from
    :class:`~app.orchestrator.service.NodeRunService`.

    A protocol rather than the concrete class because these three calls are the
    entire coupling, and C6 wraps them to add a budget and a wall clock without
    the loop learning about either. It is also the list of transitions the
    scheduler is allowed to cause: it never writes a node row itself, so the
    session projection and the ordered write path stay in one place.
    """

    async def start_node(
        self, node_id: NodeId, *, parents: Sequence[NodeId] = ()
    ) -> NodeExecution: ...

    async def block_node(
        self, node_id: NodeId, *, causes: Sequence[NodeId]
    ) -> bool: ...

    async def fail_node(self, node_id: NodeId, *, reason: str) -> bool: ...


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

    @property
    def max_concurrency(self) -> int:
        return self._max_concurrency

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
