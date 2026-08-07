"""Schema-shape assertions: the parts of the table definitions other code relies on.

Not a re-statement of the model file. Each test here pins something another
document or another activity depends on — the token fields of invariant 3, the
index `design.md` §4 names, the nullability that makes an unknown cost readable
as unknown.
"""

from pathlib import Path

import sqlalchemy as sa

from app.models.tables import Node, PathType, Run, Session, UsageEvent


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
    for table in (Node.__table__, Run.__table__, UsageEvent.__table__):
        for foreign_key in table.foreign_keys:
            assert foreign_key.ondelete == "CASCADE", f"{table.name}.{foreign_key}"


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
        Run.__table__,
        UsageEvent.__table__,
    ):
        for column in table.columns:
            if column.name.endswith("_ms") or column.name == "ts":
                assert column.server_default is None, f"{table.name}.{column.name}"
                assert column.default is None, f"{table.name}.{column.name}"
