"""The pure core: lifecycle decisions and DAG reasoning, with no I/O.

Phase 1 needed one answer to "what state follows this run?". Phase 2 adds the
DAG itself: construction, validation, a deterministic topological order,
readiness, blocked propagation, and the difference between a finished graph and
a stuck one.

Everything here is a plain function over plain values (`docs/architecture.md`
§3). No `async`, no clock, no logging, no database — which is what makes the
part of the scheduler an LLM planner will stress with strange input testable
without processes, worktrees or time.

**This module deliberately does not import the persistence model.** It takes
:data:`~app.models.ids.NodeId` and :class:`~app.models.status.NodeStatus`, both
leaf modules, and nothing else. The graph is a value the caller builds; mapping
rows onto :class:`GraphNode` belongs to the scheduler, not here. Coupling the
pure core to the `node` table would make every schema change a change to the
DAG logic and its tests.

Errors are values (`docs/architecture.md` §9). An invalid graph is expected
input — the planner produces one regularly — so :func:`build_dag` returns
:class:`InvalidDag` naming exactly what is wrong instead of raising. Exceptions
remain reserved for programmer error: a status map that does not describe the
graph it is evaluated against.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from app.models.ids import NodeId
from app.models.status import NodeStatus, RunState, SessionStatus


class RunBlockReason(StrEnum):
    PARSER_UNTRUSTED = "parser_untrusted"
    PERMISSION_DENIED = "permission_denied"
    NO_CHANGES = "no_changes"


@dataclass(frozen=True, slots=True)
class RunDisposition:
    node_status: NodeStatus
    mergeable: bool
    reason: RunBlockReason | None = None


def evaluate_run(
    status: RunState,
    *,
    trusted: bool,
    permission_denials: int,
    changed: bool,
) -> RunDisposition:
    """Turn one terminal run and its checkpoint into the next node state."""
    if status is not RunState.SUCCESS:
        return RunDisposition(NodeStatus.FAILED, mergeable=False)
    if not trusted:
        return RunDisposition(
            NodeStatus.BLOCKED,
            mergeable=False,
            reason=RunBlockReason.PARSER_UNTRUSTED,
        )
    if permission_denials:
        return RunDisposition(
            NodeStatus.BLOCKED,
            mergeable=False,
            reason=RunBlockReason.PERMISSION_DENIED,
        )
    if not changed:
        return RunDisposition(
            NodeStatus.BLOCKED,
            mergeable=False,
            reason=RunBlockReason.NO_CHANGES,
        )
    return RunDisposition(NodeStatus.AWAITING_REVIEW, mergeable=True)


def session_status_for_node(status: NodeStatus) -> SessionStatus:
    """The Phase 1 session projection for its only node."""
    if status in (NodeStatus.PENDING, NodeStatus.READY):
        return SessionStatus.PLANNING
    if status is NodeStatus.RUNNING:
        return SessionStatus.RUNNING
    if status in (NodeStatus.AWAITING_REVIEW, NodeStatus.BLOCKED):
        return SessionStatus.PAUSED
    if status in (NodeStatus.DONE, NodeStatus.SKIPPED):
        return SessionStatus.DONE
    return SessionStatus.FAILED


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One activity, reduced to what DAG reasoning needs.

    Title, harness, model, `touches` and acceptance criteria are all real node
    attributes (`design.md` §8) and none of them belong here: the topology does
    not depend on them, and carrying them would drag the persistence model into
    the pure core.
    """

    id: NodeId
    depends_on: tuple[NodeId, ...] = ()


class DagErrorKind(StrEnum):
    """What is wrong with a proposed graph.

    These are the categories `design.md` §8 step 3 requires be handed back to
    the planner. The kind selects the correction message; ``nodes`` says where.
    """

    INVALID_NODE_ID = "invalid_node_id"
    DUPLICATE_NODE = "duplicate_node"
    UNKNOWN_DEPENDENCY = "unknown_dependency"
    SELF_EDGE = "self_edge"
    DUPLICATE_EDGE = "duplicate_edge"
    CYCLE = "cycle"


@dataclass(frozen=True, slots=True)
class DagError:
    """One structural defect.

    ``nodes`` is the actionable part. For :data:`DagErrorKind.CYCLE` it is the
    cycle itself in execution order — ``("a", "b", "c")`` meaning *a before b
    before c before a again* — because "there is a cycle" gives the planner
    nothing to edit. For the edge kinds it is ``(dependent, dependency)``.
    """

    kind: DagErrorKind
    nodes: tuple[NodeId, ...]
    message: str

    @property
    def sort_key(self) -> tuple[str, tuple[NodeId, ...]]:
        return (self.kind.value, self.nodes)


@dataclass(frozen=True, slots=True)
class InvalidDag:
    """The failure half of :func:`build_dag`'s result.

    All defect categories are reported together rather than at the first one.
    C8 bounds the planner correction loop to a few attempts, so spending a
    round trip revealing one missing dependency at a time wastes the budget.
    """

    errors: tuple[DagError, ...]

    @property
    def cycles(self) -> tuple[tuple[NodeId, ...], ...]:
        """Every cycle, self-dependencies included.

        A self edge is a cycle of length one. It is reported under its own kind
        because the planner's fix differs — drop one entry from ``depends_on``
        rather than reorder a loop — but a correction loop that only asks "are
        there cycles?" must still see it.
        """
        return tuple(
            error.nodes
            for error in self.errors
            if error.kind in (DagErrorKind.CYCLE, DagErrorKind.SELF_EDGE)
        )


@dataclass(frozen=True, slots=True)
class Dag:
    """A graph that has been proven acyclic, closed and duplicate-free.

    Only :func:`build_dag` constructs one, so holding a ``Dag`` is proof the
    validation ran. Every accessor returns node ids in topological order, which
    makes the order a scheduler launches nodes in reproducible.

    The empty graph is a valid ``Dag``. Emptiness is a product question — "the
    planner returned no activities" is a planner error with a far better
    message, and C9 should refuse to create an empty graph — not a structural
    one. Rejecting it here would force every caller to special-case a graph
    being built up incrementally, and it would make :meth:`is_complete` lie
    about the vacuous case: zero nodes are all done.
    """

    order: tuple[NodeId, ...]
    _dependencies: Mapping[NodeId, tuple[NodeId, ...]]
    _dependents: Mapping[NodeId, tuple[NodeId, ...]]

    def __contains__(self, node_id: NodeId) -> bool:
        return node_id in self._dependencies

    def __len__(self) -> int:
        return len(self.order)

    def dependencies_of(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """The nodes ``node_id`` waits for — its ``depends_on``, deduplicated."""
        return self._dependencies[node_id]

    def dependents_of(self, node_id: NodeId) -> tuple[NodeId, ...]:
        return self._dependents[node_id]

    @property
    def roots(self) -> tuple[NodeId, ...]:
        return tuple(n for n in self.order if not self._dependencies[n])

    def transitive_dependents(self, node_id: NodeId) -> tuple[NodeId, ...]:
        """Everything downstream of ``node_id``, excluding itself.

        This is the reach of a failure: `docs/phase-2.md` C3 requires these end
        ``blocked`` rather than sitting ``pending`` forever.
        """
        reached: set[NodeId] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for child in self._dependents[current]:
                if child not in reached:
                    reached.add(child)
                    frontier.append(child)
        return tuple(n for n in self.order if n in reached)


class GraphOutcome(StrEnum):
    """What the scheduler loop should do next.

    ``design.md`` §9 collapses everything that is not "keep going" into a
    single ``break   # deadlock, or everything blocked``. Those are different
    facts to an operator, and one of them is not even a stall: with
    ``auto_merge`` off, a graph whose only remaining node is
    ``awaiting_review`` is working exactly as designed (invariant 6).
    """

    ACTIVE = "active"
    """At least one node is running or may start now."""

    WAITING_ON_HUMAN = "waiting_on_human"
    """Nothing can start; an ``awaiting_review`` or ``blocked`` node holds it."""

    COMPLETE = "complete"
    """Every node reached a terminal state. See :attr:`GraphEvaluation.succeeded`."""

    DEADLOCKED = "deadlocked"
    """Nothing running, nothing ready, no human gate open, and not complete.

    Reachable only when a transition was not persisted — typically dependents
    of a failed node still sitting ``pending`` instead of ``blocked``. Report
    it; it names a scheduler bug rather than a graph state.
    """


@dataclass(frozen=True, slots=True)
class BlockedNode:
    """A node that may not start because something upstream will not finish."""

    id: NodeId
    causes: tuple[NodeId, ...]
    """The ``failed``/``blocked`` ancestors responsible, nearest cause first.

    `design.md` §8's drawer shows a reason for a ``blocked`` node; "failed
    dependency" without the dependency's name is not a reason.
    """


@dataclass(frozen=True, slots=True)
class GraphEvaluation:
    """One consistent snapshot of what the graph permits, in topological order.

    The scheduler asks once per tick rather than calling four predicates that
    could each see a different status map.
    """

    outcome: GraphOutcome
    ready: tuple[NodeId, ...]
    running: tuple[NodeId, ...]
    awaiting_review: tuple[NodeId, ...]
    blocked: tuple[NodeId, ...]
    blocked_by_upstream: tuple[BlockedNode, ...]
    failed: tuple[NodeId, ...]

    @property
    def is_complete(self) -> bool:
        return self.outcome is GraphOutcome.COMPLETE

    @property
    def is_stuck(self) -> bool:
        """Nothing will happen without intervention. Not the same as finished."""
        return self.outcome in (
            GraphOutcome.WAITING_ON_HUMAN,
            GraphOutcome.DEADLOCKED,
        )

    @property
    def succeeded(self) -> bool:
        return self.is_complete and not self.failed


class _DependencyEffect(StrEnum):
    SATISFIED = "satisfied"
    OBSTRUCTED = "obstructed"
    OUTSTANDING = "outstanding"


_STARTABLE = frozenset({NodeStatus.PENDING, NodeStatus.READY})
_TERMINAL = frozenset({NodeStatus.DONE, NodeStatus.FAILED, NodeStatus.SKIPPED})


def _dependency_effect(status: NodeStatus) -> _DependencyEffect:
    """What a dependency in ``status`` means for the nodes that wait on it.

    ``skipped`` satisfies its dependents. `design.md` does not say so, and the
    opposite reading is defensible — a skipped node produced nothing — but a
    skip that blocks everything downstream is not a usable operator action, and
    :func:`session_status_for_node` already treats ``skipped`` as a settled
    success. Flagged as under-specified.

    ``awaiting_review`` does not satisfy anything: a node waiting on a human has
    not merged, and releasing its dependents would execute work the human has
    not approved (invariant 6).
    """
    match status:
        case NodeStatus.DONE | NodeStatus.SKIPPED:
            return _DependencyEffect.SATISFIED
        case NodeStatus.FAILED | NodeStatus.BLOCKED:
            return _DependencyEffect.OBSTRUCTED
        case (
            NodeStatus.PENDING
            | NodeStatus.READY
            | NodeStatus.RUNNING
            | NodeStatus.AWAITING_REVIEW
        ):
            return _DependencyEffect.OUTSTANDING
        case _:  # pragma: no cover - closed enum, guards a new member
            assert_never(status)


def build_dag(nodes: Iterable[GraphNode]) -> Dag | InvalidDag:
    """Validate a proposed graph and, if it holds, freeze it into a :class:`Dag`.

    Detects duplicate and empty node ids, ``depends_on`` pointing at a node
    that does not exist, self edges, duplicate edges, and cycles — reporting
    the members of each cycle.
    """
    listed = list(nodes)
    errors: list[DagError] = []

    known: dict[NodeId, GraphNode] = {}
    duplicated: set[NodeId] = set()
    for node in listed:
        if not node.id or node.id.strip() != node.id:
            errors.append(
                DagError(
                    DagErrorKind.INVALID_NODE_ID,
                    (node.id,),
                    f"node id {node.id!r} is empty or padded with whitespace",
                )
            )
            continue
        if node.id in known:
            duplicated.add(node.id)
            continue
        known[node.id] = node
    for node_id in duplicated:
        errors.append(
            DagError(
                DagErrorKind.DUPLICATE_NODE,
                (node_id,),
                f"node {node_id!r} is declared more than once",
            )
        )

    dependencies: dict[NodeId, tuple[NodeId, ...]] = {}
    dependents: dict[NodeId, list[NodeId]] = {node_id: [] for node_id in known}
    for node_id, node in known.items():
        accepted: list[NodeId] = []
        seen: set[NodeId] = set()
        for dependency in node.depends_on:
            if dependency == node_id:
                errors.append(
                    DagError(
                        DagErrorKind.SELF_EDGE,
                        (node_id,),
                        f"node {node_id!r} depends on itself",
                    )
                )
                continue
            if dependency not in known:
                errors.append(
                    DagError(
                        DagErrorKind.UNKNOWN_DEPENDENCY,
                        (node_id, dependency),
                        f"node {node_id!r} depends on unknown node {dependency!r}",
                    )
                )
                continue
            if dependency in seen:
                errors.append(
                    DagError(
                        DagErrorKind.DUPLICATE_EDGE,
                        (node_id, dependency),
                        f"node {node_id!r} declares {dependency!r} twice",
                    )
                )
                continue
            seen.add(dependency)
            accepted.append(dependency)
        dependencies[node_id] = tuple(sorted(accepted))

    # Cycle detection runs on the cleaned edge set: an edge to a node that does
    # not exist cannot close a loop, and reporting the same defect twice makes
    # the planner's correction ambiguous.
    for node_id, deps in dependencies.items():
        for dependency in deps:
            dependents[dependency].append(node_id)
    sorted_dependents = {
        node_id: tuple(sorted(children)) for node_id, children in dependents.items()
    }
    for cycle in _cycles(sorted_dependents):
        errors.append(
            DagError(
                DagErrorKind.CYCLE,
                cycle,
                "cycle: " + " -> ".join((*cycle, cycle[0])),
            )
        )

    if errors:
        return InvalidDag(tuple(sorted(errors, key=lambda error: error.sort_key)))

    return Dag(
        order=_topological_order(dependencies, sorted_dependents),
        _dependencies=dependencies,
        _dependents=sorted_dependents,
    )


def topological_order(dag: Dag) -> tuple[NodeId, ...]:
    """The order the graph may be executed in, identical for identical inputs."""
    return dag.order


def ready_nodes(dag: Dag, status: Mapping[NodeId, NodeStatus]) -> tuple[NodeId, ...]:
    """Nodes that may start now: not yet run, every dependency satisfied.

    ``ready`` counts alongside ``pending``. `design.md` §9's sketch tests
    ``n.status == "pending"`` only, which silently drops a node the scheduler
    already persisted as ``ready`` but had not launched when it restarted.
    """
    return evaluate_graph(dag, status).ready


def blocked_by_upstream(
    dag: Dag, status: Mapping[NodeId, NodeStatus]
) -> tuple[BlockedNode, ...]:
    """Nodes still startable whose ancestors make that impossible."""
    return evaluate_graph(dag, status).blocked_by_upstream


def evaluate_graph(dag: Dag, status: Mapping[NodeId, NodeStatus]) -> GraphEvaluation:
    """Decide what the graph permits, given every node's current status.

    Raises :exc:`ValueError` when ``status`` does not describe exactly ``dag``.
    That is programmer error rather than data (`docs/architecture.md` §9): both
    come from the same repository read, so a mismatch means the caller mixed
    two graphs, and guessing a default status would hide it.
    """
    missing = tuple(node_id for node_id in dag.order if node_id not in status)
    unknown = tuple(sorted(node_id for node_id in status if node_id not in dag))
    if missing or unknown:
        raise ValueError(
            "status map does not describe this graph: "
            f"missing={list(missing)} unknown={list(unknown)}"
        )

    ready: list[NodeId] = []
    running: list[NodeId] = []
    awaiting: list[NodeId] = []
    blocked: list[NodeId] = []
    failed: list[NodeId] = []
    obstructed: dict[NodeId, tuple[NodeId, ...]] = {}

    for node_id in dag.order:
        current = status[node_id]
        if current is NodeStatus.RUNNING:
            running.append(node_id)
        elif current is NodeStatus.AWAITING_REVIEW:
            awaiting.append(node_id)
        elif current is NodeStatus.BLOCKED:
            blocked.append(node_id)
        elif current is NodeStatus.FAILED:
            failed.append(node_id)

        if current not in _STARTABLE:
            continue

        causes: list[NodeId] = []
        outstanding = False
        for dependency in dag.dependencies_of(node_id):
            match _dependency_effect(status[dependency]):
                case _DependencyEffect.SATISFIED:
                    continue
                case _DependencyEffect.OBSTRUCTED:
                    outstanding = True
                    if dependency not in causes:
                        causes.append(dependency)
                case _DependencyEffect.OUTSTANDING:
                    outstanding = True
                    # Propagate the root cause through an ancestor that is
                    # itself only waiting because something above it failed.
                    for cause in obstructed.get(dependency, ()):
                        if cause not in causes:
                            causes.append(cause)

        if causes:
            obstructed[node_id] = tuple(causes)
        elif not outstanding:
            ready.append(node_id)

    return GraphEvaluation(
        outcome=_outcome(
            dag, status, ready=ready, running=running, gated=blocked + awaiting
        ),
        ready=tuple(ready),
        running=tuple(running),
        awaiting_review=tuple(awaiting),
        blocked=tuple(blocked),
        blocked_by_upstream=tuple(
            BlockedNode(node_id, causes) for node_id, causes in obstructed.items()
        ),
        failed=tuple(failed),
    )


def _outcome(
    dag: Dag,
    status: Mapping[NodeId, NodeStatus],
    *,
    ready: list[NodeId],
    running: list[NodeId],
    gated: list[NodeId],
) -> GraphOutcome:
    """``gated`` is every node a human has to act on: ``blocked`` or in review."""
    if all(status[node_id] in _TERMINAL for node_id in dag.order):
        return GraphOutcome.COMPLETE
    if running or ready:
        return GraphOutcome.ACTIVE
    if gated:
        return GraphOutcome.WAITING_ON_HUMAN
    return GraphOutcome.DEADLOCKED


def _topological_order(
    dependencies: Mapping[NodeId, tuple[NodeId, ...]],
    dependents: Mapping[NodeId, tuple[NodeId, ...]],
) -> tuple[NodeId, ...]:
    """Kahn's algorithm with the frontier held in a heap.

    The heap is the whole point: popping the smallest available node id makes
    the order a function of the graph alone, not of dictionary or set iteration
    order. Two runs over the same nodes supplied in different sequences produce
    the same order, so a scheduler's behaviour is reproducible in a test.
    """
    remaining = {node_id: len(deps) for node_id, deps in dependencies.items()}
    frontier = [node_id for node_id, count in remaining.items() if count == 0]
    heapq.heapify(frontier)
    order: list[NodeId] = []
    while frontier:
        node_id = heapq.heappop(frontier)
        order.append(node_id)
        for child in dependents[node_id]:
            remaining[child] -= 1
            if remaining[child] == 0:
                heapq.heappush(frontier, child)
    return tuple(order)


def _cycles(
    dependents: Mapping[NodeId, tuple[NodeId, ...]],
) -> tuple[tuple[NodeId, ...], ...]:
    """Every cycle in the graph, each as a canonical node sequence.

    Self edges are excluded — :func:`build_dag` reports those separately, with
    a message the planner can act on directly.
    """
    found: list[tuple[NodeId, ...]] = []
    for component in _strongly_connected_components(dependents):
        if len(component) < 2:
            continue
        found.append(_canonical_cycle(component, dependents))
    return tuple(sorted(found))


def _strongly_connected_components(
    dependents: Mapping[NodeId, tuple[NodeId, ...]],
) -> list[frozenset[NodeId]]:
    """Tarjan, iteratively.

    Iterative rather than recursive because the input is planner output: a
    hundred-node chain must not become a ``RecursionError`` in the pure core.
    """
    index: dict[NodeId, int] = {}
    low: dict[NodeId, int] = {}
    on_stack: set[NodeId] = set()
    stack: list[NodeId] = []
    components: list[frozenset[NodeId]] = []
    counter = 0

    for root in sorted(dependents):
        if root in index:
            continue
        work: list[tuple[NodeId, int]] = [(root, 0)]
        while work:
            node_id, cursor = work[-1]
            if cursor == 0:
                index[node_id] = counter
                low[node_id] = counter
                counter += 1
                stack.append(node_id)
                on_stack.add(node_id)

            descended = False
            children = dependents[node_id]
            while cursor < len(children):
                child = children[cursor]
                cursor += 1
                if child not in index:
                    work[-1] = (node_id, cursor)
                    work.append((child, 0))
                    descended = True
                    break
                if child in on_stack:
                    low[node_id] = min(low[node_id], index[child])
            if descended:
                continue

            work.pop()
            if low[node_id] == index[node_id]:
                component: set[NodeId] = set()
                while True:
                    popped = stack.pop()
                    on_stack.discard(popped)
                    component.add(popped)
                    if popped == node_id:
                        break
                components.append(frozenset(component))
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node_id])

    return components


def _canonical_cycle(
    component: frozenset[NodeId],
    dependents: Mapping[NodeId, tuple[NodeId, ...]],
) -> tuple[NodeId, ...]:
    """A shortest cycle through the component's lowest node id.

    A path is worth the breadth-first search: telling the planner
    ``a -> b -> c -> a`` says which edge to delete, where an unordered set of
    members does not.
    """
    start = min(component)
    previous: dict[NodeId, NodeId] = {}
    queue: list[NodeId] = [start]
    closing: NodeId | None = None
    while queue and closing is None:
        node_id = queue.pop(0)
        for child in dependents[node_id]:
            if child not in component:
                continue
            if child == start:
                closing = node_id
                break
            if child not in previous:
                previous[child] = node_id
                queue.append(child)
    if closing is None:  # pragma: no cover - an SCC of size >= 2 always closes
        return tuple(sorted(component))

    path = [closing]
    while path[-1] != start:
        path.append(previous[path[-1]])
    return tuple(reversed(path))


__all__ = [
    "BlockedNode",
    "Dag",
    "DagError",
    "DagErrorKind",
    "GraphEvaluation",
    "GraphNode",
    "GraphOutcome",
    "InvalidDag",
    "RunBlockReason",
    "RunDisposition",
    "blocked_by_upstream",
    "build_dag",
    "evaluate_graph",
    "evaluate_run",
    "ready_nodes",
    "session_status_for_node",
    "topological_order",
]
