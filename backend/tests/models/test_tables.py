"""Schema-shape assertions: the parts of the table definitions other code relies on.

Not a re-statement of the model file. Each test here pins something another
document or another activity depends on — the token fields of invariant 3, the
index `design.md` §4 names, the nullability that makes an unknown cost readable
as unknown.
"""

from pathlib import Path

import sqlalchemy as sa

from app.models.tables import (
    Node,
    NodeDependency,
    PathType,
    Run,
    Session,
    StringTupleType,
    UsageEvent,
)


def test_usage_event_has_all_four_token_fields_plus_the_tier_split() -> None:
    columns = UsageEvent.__table__.columns
    # invariant 3: input + output + cache_read + cache_write. A dashboard that
    # sums a subset is wrong by ~100x in a long session.
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ):
        assert name in columns, name
    # design.md §4 prices the two cache TTLs differently (~1.25x vs ~2.0x).
    assert "cache_write_5m_tokens" in columns
    assert "cache_write_1h_tokens" in columns


def test_cost_is_nullable_and_carries_its_price_table_version() -> None:
    columns = UsageEvent.__table__.columns
    # An unpriced model must read as unknown. 0.0 is a number someone trusts.
    assert columns["cost_usd"].nullable is True
    assert columns["price_table_version"].nullable is False


def test_usage_event_keeps_the_index_design_names() -> None:
    names = {index.name for index in UsageEvent.__table__.indexes}
    assert "ix_usage_session_ts" in names


def test_usage_event_may_belong_to_a_session_without_a_node() -> None:
    # design.md §4: node_id is nullable. The planner's own token spend belongs
    # to a session, not to an activity.
    assert UsageEvent.__table__.columns["node_id"].nullable is True
    assert UsageEvent.__table__.columns["session_id"].nullable is False


def test_a_node_cannot_have_two_runs_with_the_same_attempt() -> None:
    unique = {
        tuple(column.name for column in constraint.columns)
        for constraint in Run.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("node_id", "attempt") in unique


def test_deleting_a_session_cascades_to_everything_below_it() -> None:
    for table in (
        Node.__table__,
        NodeDependency.__table__,
        Run.__table__,
        UsageEvent.__table__,
    ):
        for foreign_key in table.foreign_keys:
            assert foreign_key.ondelete == "CASCADE", f"{table.name}.{foreign_key}"


def test_an_edge_is_a_row_and_not_a_column_on_node() -> None:
    """`docs/phase-2.md` C1: the scheduler queries edges on every transition.

    A JSON blob on ``node`` makes "which nodes are ready" a full scan plus a
    parse, and nothing in it can be constrained.
    """
    assert "depends_on" not in Node.__table__.columns
    assert {"node_id", "depends_on_id"} <= set(NodeDependency.__table__.columns.keys())


def test_a_duplicate_edge_is_impossible_by_the_primary_key() -> None:
    assert [column.name for column in NodeDependency.__table__.primary_key] == [
        "node_id",
        "depends_on_id",
    ]


def test_a_self_edge_is_refused_by_a_named_check() -> None:
    """Named so Alembic's batch mode can re-create it (see ``_status_check``)."""
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in NodeDependency.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert checks["ck_node_dependency_no_self_dependency"] == "node_id <> depends_on_id"


def test_both_ends_of_an_edge_are_pinned_to_one_session() -> None:
    """Two composite foreign keys sharing this row's single ``session_id``.

    That shared column is the whole mechanism: neither endpoint can be in a
    session other than the one the edge claims, so neither can differ from the
    other. It needs a unique index on the parent side to be a legal foreign key
    at all, which is what ``ix_node_id_session_id`` is for.
    """
    referenced = {
        (tuple(column.name for column in constraint.columns), constraint.ondelete)
        for constraint in NodeDependency.__table__.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }
    assert referenced == {
        (("node_id", "session_id"), "CASCADE"),
        (("depends_on_id", "session_id"), "CASCADE"),
    }

    parent_key = {
        tuple(column.name for column in index.columns): index.unique
        for index in Node.__table__.indexes
    }
    assert parent_key[("id", "session_id")] is True


def test_the_reverse_edge_lookup_is_indexed() -> None:
    """ "Who was waiting on the node that just finished" runs on every completion.

    The primary key already covers the forward direction.
    """
    names = {index.name for index in NodeDependency.__table__.indexes}
    assert "ix_node_dependency_depends_on_id" in names
    assert "ix_node_dependency_session_id" in names


def test_the_planners_authored_fields_are_on_the_node() -> None:
    """`design.md` §8's per-node schema, minus what is not persisted here.

    ``touches`` is the only input `design.md` §12's parallel-conflict risk has a
    mitigation from, so its absence would be silent.
    """
    columns = Node.__table__.columns
    for name in ("name", "prompt", "acceptance_criteria", "harness", "model"):
        assert name in columns, name
    assert isinstance(columns["touches"].type, StringTupleType)
    assert columns["touches"].nullable is False
    assert columns["estimated_effort"].nullable is True


def test_effort_is_advisory_and_not_a_closed_vocabulary() -> None:
    """`design.md` §8 shows "medium" and never closes the set.

    A CHECK here would turn an advisory badge into a planner response the
    correction loop has to spend a round trip on. Nothing may schedule on it.
    """
    checks = {
        constraint.name
        for constraint in Node.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert checks == {"ck_node_node_status"}


def test_a_list_of_strings_round_trips_as_an_immutable_tuple() -> None:
    string_tuple = StringTupleType()
    dialect = sa.create_engine("sqlite://").dialect
    stored = string_tuple.process_bind_param(
        ["backend/auth/**", "docs/api.md"], dialect
    )
    assert stored == '["backend/auth/**", "docs/api.md"]'
    assert string_tuple.process_result_value(stored, dialect) == (
        "backend/auth/**",
        "docs/api.md",
    )
    assert string_tuple.process_bind_param(None, dialect) is None
    assert string_tuple.process_result_value(None, dialect) is None


def test_no_run_level_token_or_cost_totals() -> None:
    """Aggregates are ``SUM()`` over an index (`docs/architecture.md` §4).

    A denormalized counter on ``run`` is a second source of truth that drifts
    from the rows it claims to summarize, and nothing detects the drift.
    """
    suspicious = [
        name
        for name in Run.__table__.columns.keys()
        if "token" in name or "cost" in name
    ]
    assert suspicious == []


def test_paths_round_trip_through_the_path_type() -> None:
    path_type = PathType()
    dialect = sa.create_engine("sqlite://").dialect
    stored = path_type.process_bind_param(
        Path("/tmp/runs/run_1/events.ndjson"), dialect
    )
    assert stored == "/tmp/runs/run_1/events.ndjson"
    assert path_type.process_result_value(stored, dialect) == Path(
        "/tmp/runs/run_1/events.ndjson"
    )
    assert path_type.process_bind_param(None, dialect) is None
    assert path_type.process_result_value(None, dialect) is None


def test_path_columns_are_declared_with_the_path_type() -> None:
    assert isinstance(Session.__table__.columns["repo_path"].type, PathType)
    assert isinstance(Node.__table__.columns["worktree_path"].type, PathType)
    assert isinstance(Run.__table__.columns["events_path"].type, PathType)


def test_timestamps_have_no_database_side_default() -> None:
    """Replay must be able to write the event's own ``ts``.

    A ``DEFAULT CURRENT_TIMESTAMP`` would stamp a rebuilt row with the moment of
    the rebuild, and the projection would stop matching the log.
    """
    for table in (
        Session.__table__,
        Node.__table__,
        NodeDependency.__table__,
        Run.__table__,
        UsageEvent.__table__,
    ):
        for column in table.columns:
            if column.name.endswith("_ms") or column.name == "ts":
                assert column.server_default is None, f"{table.name}.{column.name}"
                assert column.default is None, f"{table.name}.{column.name}"
