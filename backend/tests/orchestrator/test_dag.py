"""The pure DAG core (Phase 2 C2).

No fixtures, no I/O, no time — that is the point of the module. Cases are
table-driven because most of them differ only in the graph and the expected
verdict.
"""

import itertools
import random

import pytest

from app.models.ids import NodeId
from app.models.status import NodeStatus
from app.orchestrator.graph import (
    BlockedNode,
    Dag,
    DagErrorKind,
    GraphNode,
    GraphOutcome,
    InvalidDag,
    blocked_by_upstream,
    build_dag,
    evaluate_graph,
    ready_nodes,
    topological_order,
)


def nodes_from(spec: dict[str, list[str]]) -> list[GraphNode]:
    return [GraphNode(node_id, tuple(deps)) for node_id, deps in spec.items()]


def valid(spec: dict[str, list[str]]) -> Dag:
    result = build_dag(nodes_from(spec))
    assert isinstance(result, Dag), result
    return result


def invalid(spec: dict[str, list[str]]) -> InvalidDag:
    result = build_dag(nodes_from(spec))
    assert isinstance(result, InvalidDag), result
    return result


def statuses(dag: Dag, **overrides: NodeStatus) -> dict[NodeId, NodeStatus]:
    """Every node ``pending`` unless named otherwise."""
    state = dict.fromkeys(dag.order, NodeStatus.PENDING)
    for node_id, status in overrides.items():
        assert node_id in state, node_id
        state[node_id] = status
    return state


CHAIN = {"a": [], "b": ["a"], "c": ["b"]}
DIAMOND = {"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]}
DISCONNECTED = {"a": [], "b": ["a"], "x": [], "y": ["x"]}


def random_dag(seed: int, size: int) -> dict[str, list[str]]:
    """A random DAG: an edge only ever points from a lower index to a higher one."""
    rng = random.Random(seed)
    ids = [f"n{index:02d}" for index in range(size)]
    return {
        node_id: [candidate for candidate in ids[:index] if rng.random() < 0.35]
        for index, node_id in enumerate(ids)
    }


# --------------------------------------------------------------------------
# Construction and validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spec"),
    [
        ("empty", {}),
        ("single node", {"a": []}),
        ("chain", CHAIN),
        ("diamond", DIAMOND),
        ("disconnected components", DISCONNECTED),
        ("fan out", {"root": [], "a": ["root"], "b": ["root"], "c": ["root"]}),
        ("fan in", {"a": [], "b": [], "c": [], "sink": ["a", "b", "c"]}),
    ],
)
def test_a_well_formed_graph_builds(name: str, spec: dict[str, list[str]]) -> None:
    dag = valid(spec)
    assert len(dag) == len(spec)
    assert set(dag.order) == set(spec)


def test_the_empty_graph_is_valid_and_vacuously_complete() -> None:
    """See ``Dag``'s docstring: emptiness is a product rule, not a structural one.

    A planner returning no activities is C8's error to report, with a message
    an operator can act on. Here, zero nodes are all done.
    """
    dag = valid({})
    assert dag.order == ()
    assert dag.roots == ()
    evaluation = evaluate_graph(dag, {})
    assert evaluation.outcome is GraphOutcome.COMPLETE
    assert evaluation.succeeded is True
    assert evaluation.is_stuck is False


@pytest.mark.parametrize(
    ("name", "spec", "kind", "nodes"),
    [
        (
            "self dependency is a cycle of length one",
            {"a": ["a"]},
            DagErrorKind.SELF_EDGE,
            ("a",),
        ),
        (
            "cycle of length two",
            {"a": ["b"], "b": ["a"]},
            DagErrorKind.CYCLE,
            ("a", "b"),
        ),
        (
            "cycle of length three",
            {"a": ["c"], "b": ["a"], "c": ["b"]},
            DagErrorKind.CYCLE,
            ("a", "b", "c"),
        ),
        (
            "cycle of length five",
            {"a": ["e"], "b": ["a"], "c": ["b"], "d": ["c"], "e": ["d"]},
            DagErrorKind.CYCLE,
            ("a", "b", "c", "d", "e"),
        ),
        (
            "orphan depends_on",
            {"a": [], "b": ["ghost"]},
            DagErrorKind.UNKNOWN_DEPENDENCY,
            ("b", "ghost"),
        ),
        (
            "duplicate edge",
            {"a": [], "b": ["a", "a"]},
            DagErrorKind.DUPLICATE_EDGE,
            ("b", "a"),
        ),
        (
            "empty node id",
            {"": [], "a": []},
            DagErrorKind.INVALID_NODE_ID,
            ("",),
        ),
        (
            "padded node id",
            {" a ": []},
            DagErrorKind.INVALID_NODE_ID,
            (" a ",),
        ),
    ],
)
def test_a_malformed_graph_is_reported_not_raised(
    name: str,
    spec: dict[str, list[str]],
    kind: DagErrorKind,
    nodes: tuple[NodeId, ...],
) -> None:
    result = invalid(spec)
    assert [(error.kind, error.nodes) for error in result.errors] == [(kind, nodes)]
    assert nodes[0] in result.errors[0].message


def test_a_duplicate_node_is_reported() -> None:
    result = build_dag([GraphNode("a"), GraphNode("a"), GraphNode("b", ("a",))])
    assert isinstance(result, InvalidDag)
    assert [(error.kind, error.nodes) for error in result.errors] == [
        (DagErrorKind.DUPLICATE_NODE, ("a",))
    ]


def test_a_cycle_names_its_members_in_execution_order() -> None:
    result = invalid({"a": ["c"], "b": ["a"], "c": ["b"]})
    assert result.cycles == (("a", "b", "c"),)
    assert result.errors[0].message == "cycle: a -> b -> c -> a"


def test_a_self_dependency_is_visible_to_a_cycle_only_correction_loop() -> None:
    assert invalid({"a": ["a"]}).cycles == (("a",),)


def test_a_cycle_is_found_beside_a_valid_component() -> None:
    result = invalid(
        {
            "ok1": [],
            "ok2": ["ok1"],
            "x": ["y"],
            "y": ["x"],
        }
    )
    assert result.cycles == (("x", "y"),)


def test_two_independent_cycles_are_both_reported() -> None:
    result = invalid({"a": ["b"], "b": ["a"], "p": ["q"], "q": ["p"]})
    assert result.cycles == (("a", "b"), ("p", "q"))


def test_a_cycle_is_reported_as_a_shortest_loop_not_the_whole_component() -> None:
    # `a -> b -> a` and `a -> b -> c -> a` share a strongly connected
    # component; the shorter loop is the one worth handing back.
    result = invalid({"a": ["b", "c"], "b": ["a"], "c": ["b"]})
    assert result.cycles == (("a", "b"),)


def test_every_defect_category_is_reported_in_one_pass() -> None:
    """C8 bounds the correction loop, so one round trip must reveal everything."""
    result = invalid(
        {
            "a": ["a"],
            "b": ["ghost"],
            "c": ["a", "a"],
            "x": ["y"],
            "y": ["x"],
        }
    )
    assert {error.kind for error in result.errors} == {
        DagErrorKind.SELF_EDGE,
        DagErrorKind.UNKNOWN_DEPENDENCY,
        DagErrorKind.DUPLICATE_EDGE,
        DagErrorKind.CYCLE,
    }


def test_an_edge_to_a_missing_node_is_not_also_reported_as_a_cycle() -> None:
    result = invalid({"a": ["ghost"]})
    assert result.cycles == ()


def test_errors_are_ordered_deterministically() -> None:
    spec = {"z": ["ghost"], "a": ["ghost"], "m": ["m"]}
    first = invalid(spec)
    shuffled = dict(reversed(list(spec.items())))
    assert invalid(shuffled).errors == first.errors


@pytest.mark.parametrize("seed", range(25))
def test_build_dag_is_total_over_arbitrary_input(seed: int) -> None:
    """Never raises. Any node/edge soup is either a Dag or a typed report."""
    rng = random.Random(seed)
    ids = [f"n{index}" for index in range(rng.randint(0, 8))]
    pool = [*ids, "ghost", ""]
    soup = [
        GraphNode(
            node_id,
            tuple(rng.choice(pool) for _ in range(rng.randint(0, 3))),
        )
        for node_id in ids
        for _ in range(rng.choice([1, 1, 1, 2]))
    ]
    assert isinstance(build_dag(soup), Dag | InvalidDag)


# --------------------------------------------------------------------------
# Topological order
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spec"),
    [("chain", CHAIN), ("diamond", DIAMOND), ("disconnected", DISCONNECTED)],
)
def test_dependencies_precede_dependents(name: str, spec: dict[str, list[str]]) -> None:
    dag = valid(spec)
    position = {node_id: index for index, node_id in enumerate(dag.order)}
    for node_id in dag.order:
        for dependency in dag.dependencies_of(node_id):
            assert position[dependency] < position[node_id]


def test_topological_order_ignores_input_order() -> None:
    reference = topological_order(valid(DIAMOND))
    for permutation in itertools.permutations(DIAMOND):
        shuffled = {node_id: DIAMOND[node_id] for node_id in permutation}
        assert topological_order(valid(shuffled)) == reference


@pytest.mark.parametrize("seed", range(15))
def test_topological_order_is_stable_across_shuffled_construction(seed: int) -> None:
    spec = random_dag(seed, size=12)
    reference = valid(spec).order
    rng = random.Random(seed + 1000)
    for _ in range(5):
        keys = list(spec)
        rng.shuffle(keys)
        shuffled = {key: sorted(spec[key], key=lambda _: rng.random()) for key in keys}
        assert valid(shuffled).order == reference


def test_ties_break_on_the_node_id() -> None:
    dag = valid({"c": [], "a": [], "b": []})
    assert dag.order == ("a", "b", "c")


def test_a_dependency_declared_twice_is_stored_once_after_correction() -> None:
    dag = valid({"a": [], "b": ["a"]})
    assert dag.dependencies_of("b") == ("a",)
    assert dag.dependents_of("a") == ("b",)
    assert dag.roots == ("a",)
    assert "ghost" not in dag


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [("a", ("b", "c", "d")), ("b", ("d",)), ("c", ("d",)), ("d", ())],
)
def test_transitive_dependents(node_id: NodeId, expected: tuple[NodeId, ...]) -> None:
    assert valid(DIAMOND).transitive_dependents(node_id) == expected


# --------------------------------------------------------------------------
# Ready set
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "dependency_status", "releases"),
    [
        ("done releases", NodeStatus.DONE, True),
        ("skipped releases", NodeStatus.SKIPPED, True),
        ("awaiting_review does not release", NodeStatus.AWAITING_REVIEW, False),
        ("running does not release", NodeStatus.RUNNING, False),
        ("pending does not release", NodeStatus.PENDING, False),
        ("ready does not release", NodeStatus.READY, False),
        ("failed does not release", NodeStatus.FAILED, False),
        ("blocked does not release", NodeStatus.BLOCKED, False),
    ],
)
def test_only_a_settled_dependency_releases_its_dependent(
    name: str, dependency_status: NodeStatus, releases: bool
) -> None:
    dag = valid({"a": [], "b": ["a"]})
    ready = ready_nodes(dag, statuses(dag, a=dependency_status))
    assert ("b" in ready) is releases


def test_awaiting_review_holds_the_whole_downstream() -> None:
    """Invariant 6: a node waiting on a human has not satisfied its dependents."""
    dag = valid(CHAIN)
    state = statuses(dag, a=NodeStatus.AWAITING_REVIEW)
    evaluation = evaluate_graph(dag, state)
    assert evaluation.ready == ()
    assert evaluation.awaiting_review == ("a",)
    assert evaluation.blocked_by_upstream == ()
    assert evaluation.outcome is GraphOutcome.WAITING_ON_HUMAN


def test_a_node_already_marked_ready_stays_startable() -> None:
    """`design.md` §9 tests ``status == "pending"`` only.

    A scheduler that persists ``ready`` before launching would, after a
    restart, never see that node again.
    """
    dag = valid({"a": []})
    assert ready_nodes(dag, statuses(dag, a=NodeStatus.READY)) == ("a",)


def test_roots_are_ready_in_a_fresh_graph() -> None:
    dag = valid(DIAMOND)
    assert ready_nodes(dag, statuses(dag)) == ("a",)


def test_a_diamond_releases_both_branches_then_the_join() -> None:
    dag = valid(DIAMOND)
    after_root = statuses(dag, a=NodeStatus.DONE)
    assert ready_nodes(dag, after_root) == ("b", "c")

    one_branch = statuses(dag, a=NodeStatus.DONE, b=NodeStatus.DONE)
    assert ready_nodes(dag, one_branch) == ("c",)

    both = statuses(dag, a=NodeStatus.DONE, b=NodeStatus.DONE, c=NodeStatus.DONE)
    assert ready_nodes(dag, both) == ("d",)


def test_the_ready_set_is_returned_in_topological_order() -> None:
    dag = valid({"a": [], "b": [], "c": []})
    assert ready_nodes(dag, statuses(dag)) == dag.order


def test_evaluate_graph_rejects_a_status_map_for_another_graph() -> None:
    dag = valid({"a": []})
    with pytest.raises(ValueError, match="missing=\\['a'\\]"):
        evaluate_graph(dag, {})
    with pytest.raises(ValueError, match="unknown=\\['stray'\\]"):
        evaluate_graph(dag, {"a": NodeStatus.PENDING, "stray": NodeStatus.PENDING})


# --------------------------------------------------------------------------
# Blocked propagation
# --------------------------------------------------------------------------


def test_a_failure_blocks_its_transitive_dependents() -> None:
    dag = valid(CHAIN)
    evaluation = evaluate_graph(dag, statuses(dag, a=NodeStatus.FAILED))
    assert evaluation.blocked_by_upstream == (
        BlockedNode("b", ("a",)),
        BlockedNode("c", ("a",)),
    )
    assert evaluation.ready == ()


def test_a_failure_does_not_block_an_unrelated_branch() -> None:
    dag = valid(DISCONNECTED)
    evaluation = evaluate_graph(dag, statuses(dag, a=NodeStatus.FAILED))
    assert blocked_by_upstream(dag, statuses(dag, a=NodeStatus.FAILED)) == (
        BlockedNode("b", ("a",)),
    )
    assert evaluation.ready == ("x",)
    assert evaluation.outcome is GraphOutcome.ACTIVE


def test_a_blocked_node_blocks_downstream_the_same_way_a_failure_does() -> None:
    dag = valid(CHAIN)
    evaluation = evaluate_graph(dag, statuses(dag, a=NodeStatus.BLOCKED))
    assert evaluation.blocked_by_upstream == (
        BlockedNode("b", ("a",)),
        BlockedNode("c", ("a",)),
    )


def test_one_branch_of_a_diamond_failing_blocks_only_the_join() -> None:
    dag = valid(DIAMOND)
    state = statuses(dag, a=NodeStatus.DONE, b=NodeStatus.FAILED)
    evaluation = evaluate_graph(dag, state)
    assert evaluation.ready == ("c",)
    assert evaluation.blocked_by_upstream == (BlockedNode("d", ("b",)),)


def test_a_blocked_node_names_every_responsible_ancestor() -> None:
    dag = valid({"a": [], "b": [], "c": ["a", "b"]})
    state = statuses(dag, a=NodeStatus.FAILED, b=NodeStatus.BLOCKED)
    assert evaluate_graph(dag, state).blocked_by_upstream == (
        BlockedNode("c", ("a", "b")),
    )


def test_the_named_cause_survives_a_chain_of_pending_nodes() -> None:
    dag = valid(CHAIN)
    evaluation = evaluate_graph(dag, statuses(dag, a=NodeStatus.FAILED))
    assert dict(
        (blocked.id, blocked.causes) for blocked in evaluation.blocked_by_upstream
    ) == {"b": ("a",), "c": ("a",)}


def test_applying_the_propagation_is_a_fixed_point() -> None:
    """What C3 will do: mark them blocked, then evaluate again."""
    dag = valid(DIAMOND)
    state = statuses(dag, a=NodeStatus.FAILED)
    first = evaluate_graph(dag, state)
    for blocked in first.blocked_by_upstream:
        state[blocked.id] = NodeStatus.BLOCKED
    second = evaluate_graph(dag, state)
    assert second.blocked_by_upstream == ()
    assert second.blocked == ("b", "c", "d")
    assert second.outcome is GraphOutcome.WAITING_ON_HUMAN


# --------------------------------------------------------------------------
# Completion and deadlock
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "spec", "overrides", "outcome"),
    [
        ("fresh graph", CHAIN, {}, GraphOutcome.ACTIVE),
        (
            "node running",
            CHAIN,
            {"a": NodeStatus.RUNNING},
            GraphOutcome.ACTIVE,
        ),
        (
            "everything done",
            CHAIN,
            {
                "a": NodeStatus.DONE,
                "b": NodeStatus.DONE,
                "c": NodeStatus.DONE,
            },
            GraphOutcome.COMPLETE,
        ),
        (
            "terminal with a failure",
            {"a": [], "b": []},
            {"a": NodeStatus.DONE, "b": NodeStatus.FAILED},
            GraphOutcome.COMPLETE,
        ),
        (
            "skipped counts as terminal",
            {"a": []},
            {"a": NodeStatus.SKIPPED},
            GraphOutcome.COMPLETE,
        ),
        (
            "human gate holds the graph",
            CHAIN,
            {"a": NodeStatus.AWAITING_REVIEW},
            GraphOutcome.WAITING_ON_HUMAN,
        ),
        (
            "a blocked node holds the graph",
            CHAIN,
            {
                "a": NodeStatus.BLOCKED,
                "b": NodeStatus.BLOCKED,
                "c": NodeStatus.BLOCKED,
            },
            GraphOutcome.WAITING_ON_HUMAN,
        ),
        (
            "running beats a pending human gate",
            {"a": [], "b": []},
            {"a": NodeStatus.RUNNING, "b": NodeStatus.AWAITING_REVIEW},
            GraphOutcome.ACTIVE,
        ),
        (
            "unpropagated failure is a deadlock",
            CHAIN,
            {"a": NodeStatus.FAILED},
            GraphOutcome.DEADLOCKED,
        ),
    ],
)
def test_outcome(
    name: str,
    spec: dict[str, list[str]],
    overrides: dict[str, NodeStatus],
    outcome: GraphOutcome,
) -> None:
    dag = valid(spec)
    assert evaluate_graph(dag, statuses(dag, **overrides)).outcome is outcome


def test_finished_and_stuck_are_different_answers() -> None:
    dag = valid(CHAIN)
    finished = evaluate_graph(
        dag,
        statuses(dag, a=NodeStatus.DONE, b=NodeStatus.DONE, c=NodeStatus.DONE),
    )
    stuck = evaluate_graph(dag, statuses(dag, a=NodeStatus.AWAITING_REVIEW))

    assert (finished.is_complete, finished.is_stuck) == (True, False)
    assert (stuck.is_complete, stuck.is_stuck) == (False, True)


def test_a_failed_node_makes_a_complete_graph_unsuccessful() -> None:
    dag = valid({"a": []})
    evaluation = evaluate_graph(dag, statuses(dag, a=NodeStatus.FAILED))
    assert evaluation.is_complete is True
    assert evaluation.succeeded is False
    assert evaluation.failed == ("a",)


def test_a_deadlock_reports_which_nodes_are_stuck_and_why() -> None:
    """`design.md` §9 only says ``break``; the operator needs the reason."""
    dag = valid(CHAIN)
    evaluation = evaluate_graph(dag, statuses(dag, a=NodeStatus.FAILED))
    assert evaluation.outcome is GraphOutcome.DEADLOCKED
    assert evaluation.is_stuck is True
    assert evaluation.blocked_by_upstream == (
        BlockedNode("b", ("a",)),
        BlockedNode("c", ("a",)),
    )


@pytest.mark.parametrize("seed", range(15))
def test_a_graph_driven_to_completion_never_deadlocks(seed: int) -> None:
    """Drive a random DAG the way C3 will and assert the loop always terminates."""
    dag = valid(random_dag(seed, size=10))
    state = statuses(dag)
    seen: list[GraphOutcome] = []
    for _ in range(len(dag) * 3 + 2):
        evaluation = evaluate_graph(dag, state)
        seen.append(evaluation.outcome)
        if evaluation.is_complete:
            break
        assert evaluation.outcome is GraphOutcome.ACTIVE
        for node_id in evaluation.ready:
            state[node_id] = NodeStatus.DONE
    assert seen[-1] is GraphOutcome.COMPLETE
