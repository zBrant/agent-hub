"""A session owns a DAG, and SQLite is what keeps it a DAG-shaped one.

`docs/phase-2.md` C1 asks for a multi-node session with edges, a self-edge and a
duplicate edge rejected **at the database level**. That last part is the point of
most of this file: every rejection below is asserted as an ``IntegrityError``
carrying the name of the constraint that produced it, because a check that only
exists in :mod:`app.storage.repository` is a check the ``sqlite3`` CLI, a
maintenance script and the next activity's bulk insert all walk straight past.

What is *not* here: cycles. A two-node cycle is inserted on purpose and expected
to be accepted — detecting it is C2's, in the pure core, where the planner can be
handed a typed error instead of an exception (`design.md` §8).
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models.ids import new_node_id, new_session_id
from app.models.status import NodeStatus
from app.models.tables import Node, Session
from app.storage.db import Database
from app.storage.repository import Repository, RepositoryError, repository

AT_MS = 1_700_000_000_000


async def make_session(repo: Repository, title: str = "second") -> Session:
    session_id = new_session_id()
    return await repo.create_session(
        session_id=session_id,
        title=title,
        repo_path=Path("/tmp/target-repo"),
        workspace_root=Path(f"/tmp/workspaces/{session_id}"),
        integration_branch=f"agenthub/{session_id}/integration",
        at_ms=AT_MS,
    )


async def make_node(
    repo: Repository,
    session: Session,
    name: str,
    *,
    status: NodeStatus = NodeStatus.PENDING,
    touches: Sequence[str] = (),
    estimated_effort: str | None = None,
) -> Node:
    return await repo.create_node(
        node_id=new_node_id(),
        session_id=session.id,
        name=name,
        prompt=f"do {name}",
        harness="codex",
        status=status,
        touches=touches,
        estimated_effort=estimated_effort,
        at_ms=AT_MS,
    )


@contextmanager
def counting_statements(database: Database) -> Iterator[list[str]]:
    """Record every statement the engine actually sends to SQLite."""
    statements: list[str] = []
    engine = database.engine.sync_engine

    def record(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    sa.event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        sa.event.remove(engine, "before_cursor_execute", record)


async def test_a_multi_node_session_with_edges_round_trips(
    repo: Repository, session_row: Session
) -> None:
    """The diamond of `docs/phase-2.md` C3: a -> {b, c} -> d."""
    a = await make_node(repo, session_row, "schema")
    b = await make_node(repo, session_row, "api")
    c = await make_node(repo, session_row, "ui")
    d = await make_node(repo, session_row, "docs")

    await repo.add_dependencies(b.id, [a.id], at_ms=AT_MS)
    await repo.add_dependencies(c.id, [a.id], at_ms=AT_MS)
    await repo.add_dependencies(d.id, [b.id, c.id], at_ms=AT_MS)

    graph = await repo.load_graph(session_row.id)
    assert graph is not None
    assert set(graph.node_ids) == {a.id, b.id, c.id, d.id}
    assert graph.depends_on() == {
        a.id: frozenset(),
        b.id: frozenset({a.id}),
        c.id: frozenset({a.id}),
        d.id: frozenset({b.id, c.id}),
    }
    # The same edges from the other end — what the scheduler reads when a node
    # finishes and it needs to know who just became reachable.
    assert graph.dependents() == {
        a.id: frozenset({b.id, c.id}),
        b.id: frozenset({d.id}),
        c.id: frozenset({d.id}),
        d.id: frozenset(),
    }
    assert graph.session.id == session_row.id


async def test_the_adjacency_views_are_total(
    repo: Repository, session_row: Session
) -> None:
    """Every node has an entry, so no caller needs ``.get(node_id, set())``."""
    lonely = await make_node(repo, session_row, "alone")
    graph = await repo.load_graph(session_row.id)
    assert graph is not None
    assert graph.depends_on()[lonely.id] == frozenset()
    assert graph.dependents()[lonely.id] == frozenset()


async def test_touches_and_estimated_effort_round_trip(
    repo: Repository, database: Database, session_row: Session
) -> None:
    """`design.md` §8's authored planner fields, read back from SQLite.

    A tuple, not a list: an in-place ``append`` on a JSON-backed column is a
    change SQLAlchemy cannot see, and the row would silently not be written.
    """
    node = await make_node(
        repo,
        session_row,
        "auth",
        touches=["backend/auth/**", "tests/test_auth.py"],
        estimated_effort="medium",
    )

    async with repository(database) as fresh:
        stored = await fresh.get_node(node.id)
    assert stored is not None
    assert stored.touches == ("backend/auth/**", "tests/test_auth.py")
    assert isinstance(stored.touches, tuple)
    assert stored.estimated_effort == "medium"


async def test_touches_defaults_to_empty_rather_than_null(
    repo: Repository, database: Database, session_row: Session
) -> None:
    node = await make_node(repo, session_row, "quiet")
    async with repository(database) as fresh:
        stored = await fresh.get_node(node.id)
    assert stored is not None
    assert stored.touches == ()
    assert stored.estimated_effort is None


async def test_a_node_cannot_depend_on_itself(
    repo: Repository, session_row: Session
) -> None:
    """A cycle of length one, refused by SQLite and not by Python."""
    node = await make_node(repo, session_row, "solo")
    with pytest.raises(IntegrityError, match="ck_node_dependency_no_self_dependency"):
        await repo.add_dependency(node.id, node.id, at_ms=AT_MS)


async def test_the_same_edge_cannot_be_added_twice(
    repo: Repository, session_row: Session
) -> None:
    """The composite primary key. A repeated edge doubles any count over them."""
    a = await make_node(repo, session_row, "first")
    b = await make_node(repo, session_row, "second")
    # Read the ids now: the rollback below expires every loaded row, and an
    # expired attribute on an async session cannot refresh itself lazily.
    session_id, parent, child = session_row.id, a.id, b.id
    await repo.add_dependency(child, parent, at_ms=AT_MS)

    with pytest.raises(IntegrityError, match=r"UNIQUE constraint failed"):
        await repo.add_dependency(child, parent, at_ms=AT_MS)
    await repo.session.rollback()

    graph = await repo.load_graph(session_id)
    assert graph is not None
    assert len(graph.edges) == 1


async def test_an_edge_cannot_cross_sessions(
    repo: Repository, session_row: Session
) -> None:
    """Both composite foreign keys resolve against one ``session_id`` column.

    An edge into another session's node would make one scheduler wait on a node
    it does not own, and validating each session's DAG separately would never
    reveal it — neither graph contains both ends.
    """
    mine = await make_node(repo, session_row, "mine")
    other_session = await make_session(repo)
    theirs = await make_node(repo, other_session, "theirs")
    session_id, here, there = session_row.id, mine.id, theirs.id

    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await repo.add_dependency(here, there, at_ms=AT_MS)
    await repo.session.rollback()

    graph = await repo.load_graph(session_id)
    assert graph is not None
    assert graph.edges == ()


async def test_an_edge_to_a_node_that_does_not_exist_is_rejected(
    repo: Repository, session_row: Session
) -> None:
    """§8's "orphan depends_on" cannot survive persistence.

    It is real, but only in the planner's JSON, before any of it is a row: here
    the foreign key answers it.
    """
    node = await make_node(repo, session_row, "real")
    real = node.id
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        await repo.add_dependency(real, new_node_id(), at_ms=AT_MS)
    await repo.session.rollback()

    # And the dependent side is a plain programmer error: nothing to hang the
    # edge's session_id off.
    with pytest.raises(RepositoryError, match="no such node"):
        await repo.add_dependency(new_node_id(), real, at_ms=AT_MS)


async def test_a_cycle_is_not_the_databases_problem(
    repo: Repository, session_row: Session
) -> None:
    """Two nodes waiting on each other insert cleanly. That is on purpose.

    A recursive CTE in a trigger could refuse it, but the planner needs the
    cycle as a typed error to hand back to the model (`design.md` §8) rather
    than an ``IntegrityError`` from the middle of writing a proposal — and
    C2 tests it exhaustively without a database at all.
    """
    a = await make_node(repo, session_row, "a")
    b = await make_node(repo, session_row, "b")
    await repo.add_dependency(a.id, b.id, at_ms=AT_MS)
    await repo.add_dependency(b.id, a.id, at_ms=AT_MS)

    graph = await repo.load_graph(session_row.id)
    assert graph is not None
    assert len(graph.edges) == 2


async def test_a_nodes_whole_edge_set_lands_or_none_of_it_does(
    repo: Repository, session_row: Session
) -> None:
    """One commit for one ``depends_on`` array (`design.md` §8).

    Half of a node's dependencies is a graph the scheduler would run too early.
    """
    a = await make_node(repo, session_row, "a")
    b = await make_node(repo, session_row, "b")
    session_id, parent, child = session_row.id, a.id, b.id

    with pytest.raises(IntegrityError):
        await repo.add_dependencies(child, [parent, new_node_id()], at_ms=AT_MS)
    await repo.session.rollback()

    graph = await repo.load_graph(session_id)
    assert graph is not None
    assert graph.edges == ()


async def test_removing_an_edge(repo: Repository, session_row: Session) -> None:
    a = await make_node(repo, session_row, "a")
    b = await make_node(repo, session_row, "b")
    await repo.add_dependency(b.id, a.id, at_ms=AT_MS)

    assert await repo.remove_dependency(b.id, a.id, at_ms=AT_MS + 5) is True
    assert await repo.remove_dependency(b.id, a.id, at_ms=AT_MS + 5) is False

    graph = await repo.load_graph(session_row.id)
    assert graph is not None
    assert graph.edges == ()
    # Both nodes survive the edit; only the edge went.
    assert set(graph.node_ids) == {a.id, b.id}


async def test_changing_the_graphs_shape_stamps_the_dependent_node(
    repo: Repository, session_row: Session
) -> None:
    """A removed edge leaves no row to carry a timestamp, so the node does.

    Without this the only record of when a graph was last edited would be the
    ``created_ms`` of edges that still exist.
    """
    a = await make_node(repo, session_row, "a")
    b = await make_node(repo, session_row, "b")
    assert b.updated_ms == AT_MS

    await repo.add_dependency(b.id, a.id, at_ms=AT_MS + 1)
    assert (await repo.get_node(b.id)).updated_ms == AT_MS + 1  # type: ignore[union-attr]
    # The node it now waits for is unchanged: the edge belongs to the dependent.
    assert (await repo.get_node(a.id)).updated_ms == AT_MS  # type: ignore[union-attr]

    await repo.remove_dependency(b.id, a.id, at_ms=AT_MS + 2)
    assert (await repo.get_node(b.id)).updated_ms == AT_MS + 2  # type: ignore[union-attr]


async def test_deleting_a_node_removes_its_edges_in_both_directions(
    repo: Repository, database: Database, session_row: Session
) -> None:
    """A graph can never hold an edge to a node that is gone.

    Both composite foreign keys cascade, so the middle of a chain can be cut out
    without leaving either of its neighbours pointing at nothing.
    """
    a = await make_node(repo, session_row, "a")
    b = await make_node(repo, session_row, "b")
    c = await make_node(repo, session_row, "c")
    survivor = await make_node(repo, session_row, "unrelated")
    other = await make_node(repo, session_row, "unrelated-parent")
    await repo.add_dependency(b.id, a.id, at_ms=AT_MS)
    await repo.add_dependency(c.id, b.id, at_ms=AT_MS)
    await repo.add_dependency(survivor.id, other.id, at_ms=AT_MS)

    assert await repo.delete_node(b.id) is True
    assert await repo.delete_node(b.id) is False

    async with repository(database) as fresh:
        graph = await fresh.load_graph(session_row.id)
    assert graph is not None
    assert set(graph.node_ids) == {a.id, c.id, survivor.id, other.id}
    # b was both a dependent and a dependency; neither edge survived, and the
    # unrelated one is untouched.
    assert [(e.node_id, e.depends_on_id) for e in graph.edges] == [
        (survivor.id, other.id)
    ]


async def test_deleting_a_session_takes_the_whole_graph(
    repo: Repository, database: Database, session_row: Session
) -> None:
    a = await make_node(repo, session_row, "a")
    b = await make_node(repo, session_row, "b")
    await repo.add_dependency(b.id, a.id, at_ms=AT_MS)

    await repo.session.delete(session_row)
    await repo.session.commit()

    async with repository(database) as fresh:
        assert await fresh.load_graph(session_row.id) is None
        assert await fresh.list_dependencies(session_row.id) == []


async def test_load_graph_of_an_unknown_session_is_none(repo: Repository) -> None:
    assert await repo.load_graph(new_session_id()) is None


async def test_a_session_with_no_nodes_is_an_empty_graph_not_a_missing_one(
    repo: Repository, session_row: Session
) -> None:
    """A session in ``planning`` whose proposal has not arrived is a real state."""
    graph = await repo.load_graph(session_row.id)
    assert graph is not None
    assert graph.nodes == ()
    assert graph.edges == ()
    assert graph.depends_on() == {}


async def test_the_whole_graph_loads_in_a_bounded_number_of_queries(
    repo: Repository, database: Database, session_row: Session
) -> None:
    """The scheduler calls this on every transition; N+1 would make it quadratic.

    Proven by comparing a small graph against a large one: the count has to be
    the *same*, not merely small, or the cost still grows with the graph.
    """
    small = [await make_node(repo, session_row, f"s{i}") for i in range(3)]
    for child in small[1:]:
        await repo.add_dependency(child.id, small[0].id, at_ms=AT_MS)

    big_session = await make_session(repo, title="big")
    big = [await make_node(repo, big_session, f"b{i}") for i in range(30)]
    for child in big[1:]:
        await repo.add_dependency(child.id, big[0].id, at_ms=AT_MS)

    async with repository(database) as fresh:
        with counting_statements(database) as small_statements:
            small_graph = await fresh.load_graph(session_row.id)
    async with repository(database) as fresh:
        with counting_statements(database) as big_statements:
            big_graph = await fresh.load_graph(big_session.id)

    assert small_graph is not None and big_graph is not None
    assert len(small_graph.nodes) == 3 and len(big_graph.nodes) == 30
    assert len(big_statements) == len(small_statements)
    # session + nodes + edges.
    assert len(small_statements) == 3, small_statements


async def test_nodes_can_be_listed_by_status(
    repo: Repository, session_row: Session
) -> None:
    ready = await make_node(repo, session_row, "ready", status=NodeStatus.READY)
    running = await make_node(repo, session_row, "running", status=NodeStatus.RUNNING)
    await make_node(repo, session_row, "pending")

    listed = await repo.list_nodes_by_status(
        [NodeStatus.READY, NodeStatus.RUNNING], session_id=session_row.id
    )
    assert [node.id for node in listed] == sorted([ready.id, running.id])

    # Restart recovery does not know which session it is looking for.
    other_session = await make_session(repo)
    orphan = await make_node(repo, other_session, "orphan", status=NodeStatus.RUNNING)
    everywhere = await repo.list_nodes_by_status([NodeStatus.RUNNING])
    assert {node.id for node in everywhere} == {running.id, orphan.id}


async def test_listing_by_no_status_is_not_listing_everything(
    repo: Repository, session_row: Session
) -> None:
    await make_node(repo, session_row, "a")
    assert await repo.list_nodes_by_status([]) == []
