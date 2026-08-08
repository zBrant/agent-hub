"""What the persistence layer promises the rest of Phase 1.

`docs/phase-1.md` B2 names four things to prove: Alembic builds an empty
database, foreign keys are enforced, ``usage_event`` is append-only, and a retry
produces several runs for one node. The first two live in ``test_migrations.py``
and ``test_db.py``; the rest are here, with the token and cost guarantees of
invariant 3.
"""

import asyncio
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError, OperationalError

from app.harnesses.events import (
    PermissionDenial,
    RunFinished,
    RunStarted,
    TurnFinished,
    Usage,
)
from app.models.ids import new_node_id, new_run_id
from app.models.pricing import PriceTable, TokenCounts
from app.models.status import NodeStatus, RunState, SessionStatus, UsageSource
from app.models.tables import Node, Run, Session
from app.storage.db import Database
from app.storage.repository import (
    Repository,
    RepositoryError,
    count_permission_denials,
    repository,
)

MODEL = "gpt-5.6-terra"
UNPRICED_MODEL = "some-model-we-have-never-seen"


def usage(**overrides: object) -> Usage:
    """One reported turn's tokens. Cache-heavy, like every real agentic turn."""
    payload: dict[str, object] = {
        "run_id": "run_placeholder",
        "ts": 1_700_000_000_000,
        "model": MODEL,
        "input_tokens": 1_200,
        "output_tokens": 350,
        "cache_read_tokens": 480_000,
        "cache_write_tokens": 9_000,
        "cache_write_5m_tokens": 1_000,
        "cache_write_1h_tokens": 8_000,
    }
    payload.update(overrides)
    return Usage.model_validate(payload)


async def another_run(repo: Repository, node: Node) -> Run:
    run_id = new_run_id()
    return await repo.create_run(
        run_id=run_id,
        node_id=node.id,
        events_path=Path("/tmp/agenthub-test/runs") / run_id / "events.ndjson",
    )


async def test_session_node_run_round_trip(
    repo: Repository, session_row: Session, node_row: Node, run_row: Run
) -> None:
    assert (await repo.get_session(session_row.id)) is not None
    assert [n.id for n in await repo.list_nodes(session_row.id)] == [node_row.id]
    assert [r.id for r in await repo.list_runs(node_row.id)] == [run_row.id]
    # The run inherits the node's brief unless told otherwise.
    assert run_row.harness == node_row.harness
    assert run_row.model == node_row.model
    assert run_row.session_id == session_row.id
    assert run_row.status is RunState.RUNNING


async def test_paths_come_back_as_paths(
    repo: Repository, session_row: Session, node_row: Node
) -> None:
    await repo.attach_worktree(
        node_row.id,
        worktree_path=Path("/tmp/workspaces/sess/node_a"),
        branch="agenthub/sess/node_a",
        base_ref="agenthub/sess/integration",
    )

    stored = await repo.get_node(node_row.id)
    assert stored is not None
    assert stored.worktree_path == Path("/tmp/workspaces/sess/node_a")
    assert isinstance(stored.worktree_path, Path)

    reloaded = await repo.get_session(session_row.id)
    assert reloaded is not None
    assert isinstance(reloaded.repo_path, Path)


async def test_statuses_persist_as_their_wire_values(
    repo: Repository, database: Database, session_row: Session, node_row: Node
) -> None:
    """The frontend keys colours off these strings (`docs/design-system.md` §5)."""
    await repo.set_node_status(node_row.id, NodeStatus.AWAITING_REVIEW)
    await repo.set_session_status(session_row.id, SessionStatus.RUNNING)

    async with database.engine.connect() as connection:
        stored = (await connection.execute(sa.text("SELECT status FROM node"))).scalar()
    assert stored == "awaiting_review"

    refreshed = await repo.get_node(node_row.id)
    assert refreshed is not None
    assert refreshed.status is NodeStatus.AWAITING_REVIEW


async def test_an_unknown_status_is_rejected_by_the_database(
    database: Database, node_row: Node
) -> None:
    async with database.engine.begin() as connection:
        with pytest.raises(IntegrityError):
            await connection.execute(sa.text("UPDATE node SET status = 'almost_done'"))


async def test_a_status_change_stamps_the_row_and_appends_one_transition(
    repo: Repository, database: Database, node_row: Node
) -> None:
    updated = await repo.set_node_status(node_row.id, NodeStatus.RUNNING, at_ms=4_242)
    # Replay passes the event's ts, which is what makes a rebuilt row identical.
    assert updated.updated_ms == 4_242
    assert updated.created_ms == node_row.created_ms

    # Reasserting a projection may refresh its timestamp during recovery, but
    # it is not a second transition in the operator's activity feed.
    await repo.set_node_status(node_row.id, NodeStatus.RUNNING, at_ms=4_243)
    async with database.engine.connect() as connection:
        transitions = (
            await connection.execute(
                sa.text(
                    "SELECT status, ts FROM node_transition WHERE node_id = :node_id"
                ),
                {"node_id": node_row.id},
            )
        ).all()
    assert transitions == [("running", 4_242)]


async def test_a_retry_is_a_new_run_not_a_mutated_one(
    repo: Repository, node_row: Node, run_row: Run
) -> None:
    """`design.md` §5 and B7: the node persists, the attempt history accumulates."""
    await repo.finish_run(
        run_row.id, RunFinished(run_id=run_row.id, ts=1, status="failed", exit_code=1)
    )
    second = await another_run(repo, node_row)

    runs = await repo.list_runs(node_row.id)
    assert [r.attempt for r in runs] == [1, 2]
    assert [r.id for r in runs] == [run_row.id, second.id]
    # The first attempt is untouched: that is the record that it was ever hard.
    assert runs[0].status is RunState.FAILED
    assert runs[0].exit_code == 1
    assert runs[1].status is RunState.RUNNING
    # And the node is still one node.
    assert len(await repo.list_nodes(node_row.session_id)) == 1


async def test_two_runs_cannot_share_an_attempt_number(
    repo: Repository, node_row: Node, run_row: Run
) -> None:
    with pytest.raises(IntegrityError):
        await repo.create_run(
            run_id=new_run_id(),
            node_id=node_row.id,
            events_path=Path("/tmp/runs/dup/events.ndjson"),
            attempt=run_row.attempt,
        )


async def test_a_retry_may_switch_harness(repo: Repository, node_row: Node) -> None:
    """``harness`` is data, not a conditional (invariant 1) — including here."""
    run = await repo.create_run(
        run_id=new_run_id(),
        node_id=node_row.id,
        events_path=Path("/tmp/runs/y/events.ndjson"),
        harness="claude-code",
        model="claude-haiku-4-5",
    )
    assert run.harness == "claude-code"
    # The node's own brief is untouched by one attempt's override.
    stored = await repo.get_node(node_row.id)
    assert stored is not None
    assert stored.harness == "codex"


async def test_session_runs_are_listed_across_nodes(
    repo: Repository, session_row: Session, node_row: Node, run_row: Run
) -> None:
    second = await another_run(repo, node_row)
    listed = await repo.list_session_runs(session_row.id)
    assert [r.id for r in listed] == sorted([run_row.id, second.id])


async def test_run_for_an_unknown_node_is_a_programmer_error(repo: Repository) -> None:
    with pytest.raises(RepositoryError, match="no such node"):
        await repo.create_run(
            run_id=new_run_id(),
            node_id=new_node_id(),
            events_path=Path("/tmp/runs/x/events.ndjson"),
        )


async def test_run_lifecycle_is_projected_from_events(
    repo: Repository, run_row: Run
) -> None:
    await repo.start_run(
        run_row.id,
        RunStarted(
            run_id=run_row.id,
            ts=1_000,
            harness="codex",
            model=MODEL,
            cwd=Path("/tmp/workspaces/sess/node_a"),
            pid=4242,
            session_id="thread_abc",
            harness_version="0.9.1",
        ),
    )
    turns = [
        TurnFinished(
            run_id=run_row.id,
            ts=1_500,
            turn=1,
            status="success",
            permission_denials=(PermissionDenial(tool="Write"),),
        )
    ]
    await repo.finish_run(
        run_row.id,
        RunFinished(run_id=run_row.id, ts=2_000, status="success", exit_code=0),
        event_count=17,
        permission_denial_count=count_permission_denials(turns),
    )

    stored = await repo.get_run(run_row.id)
    assert stored is not None
    assert stored.pid == 4242
    assert stored.harness_session_id == "thread_abc"
    assert stored.harness_version == "0.9.1"
    assert stored.cwd == Path("/tmp/workspaces/sess/node_a")
    assert stored.started_ms == 1_000
    assert stored.finished_ms == 2_000
    assert stored.event_count == 17
    assert stored.status is RunState.SUCCESS
    # Reported success with a refusal in it: B4 must not merge this run.
    assert stored.permission_denial_count == 1


async def test_an_orphan_run_becomes_interrupted(
    repo: Repository, run_row: Run
) -> None:
    assert [r.id for r in await repo.list_unfinished_runs()] == [run_row.id]

    await repo.mark_run_interrupted(
        run_row.id,
        at_ms=9_999,
        summary="pid 4242 is gone",
        event_count=17,
        permission_denial_count=2,
    )

    stored = await repo.get_run(run_row.id)
    assert stored is not None
    assert stored.status is RunState.INTERRUPTED
    assert stored.finished_ms == 9_999
    assert stored.event_count == 17
    assert stored.permission_denial_count == 2
    assert await repo.list_unfinished_runs() == []


async def test_all_four_token_fields_and_the_tier_split_round_trip(
    repo: Repository, run_row: Run, node_row: Node, prices: PriceTable
) -> None:
    event = usage()
    stored = await repo.append_usage(run_row.id, event, prices=prices)

    assert stored.input_tokens == event.input_tokens
    assert stored.output_tokens == event.output_tokens
    assert stored.cache_read_tokens == event.cache_read_tokens
    assert stored.cache_write_tokens == event.cache_write_tokens
    assert stored.cache_write_5m_tokens == event.cache_write_5m_tokens
    assert stored.cache_write_1h_tokens == event.cache_write_1h_tokens
    # Denormalized from the run, because Usage carries no harness (invariant 1).
    assert stored.harness == run_row.harness
    assert stored.node_id == node_row.id
    assert stored.session_id == run_row.session_id
    assert stored.source is UsageSource.REPORTED
    assert stored.seq == 0

    totals = await repo.usage_totals(run_id=run_row.id)
    assert totals.counts.total == (
        event.input_tokens
        + event.output_tokens
        + event.cache_read_tokens
        + event.cache_write_tokens
    )
    # The field that makes the difference between 4K and 480K in a session.
    assert totals.counts.cache_read_tokens == 480_000


async def test_reconstructed_usage_keeps_its_provenance(
    repo: Repository, run_row: Run, prices: PriceTable
) -> None:
    stored = await repo.append_usage(
        run_row.id, usage(source="reconstructed"), prices=prices
    )
    assert stored.source is UsageSource.RECONSTRUCTED


async def test_cost_is_computed_at_ingest_and_records_its_price_version(
    repo: Repository, run_row: Run, prices: PriceTable
) -> None:
    event = usage()
    stored = await repo.append_usage(run_row.id, event, prices=prices)

    expected = prices.cost_usd(
        MODEL,
        TokenCounts(
            input_tokens=event.input_tokens,
            output_tokens=event.output_tokens,
            cache_read_tokens=event.cache_read_tokens,
            cache_write_tokens=event.cache_write_tokens,
            cache_write_5m_tokens=event.cache_write_5m_tokens,
            cache_write_1h_tokens=event.cache_write_1h_tokens,
        ),
    )
    assert stored.cost_usd == expected
    assert stored.price_table_version == prices.version


async def test_a_price_change_does_not_rewrite_history(
    repo: Repository, run_row: Run, prices: PriceTable
) -> None:
    """Invariant 3: cost history must not shift when a vendor changes prices."""
    old = await repo.append_usage(run_row.id, usage(), prices=prices)

    dearer = PriceTable.from_mapping(
        {"version": 99, "models": {MODEL: {"input": 100.0, "output": 500.0}}}
    )
    new = await repo.append_usage(run_row.id, usage(), prices=dearer)

    reread = await repo.list_usage(run_row.id)
    assert reread[0].cost_usd == old.cost_usd
    assert reread[0].price_table_version == prices.version
    assert reread[1].price_table_version == 99
    assert old.cost_usd is not None and new.cost_usd is not None
    assert new.cost_usd > old.cost_usd


async def test_an_unpriced_model_costs_unknown_not_zero(
    repo: Repository, run_row: Run, prices: PriceTable
) -> None:
    stored = await repo.append_usage(
        run_row.id, usage(model=UNPRICED_MODEL), prices=prices
    )
    assert stored.cost_usd is None

    totals = await repo.usage_totals(run_id=run_row.id)
    assert totals.cost_usd is None
    assert totals.unpriced_events == 1
    assert totals.complete is False
    # The tokens still count: unknown price, known volume.
    assert totals.counts.input_tokens == 1_200


async def test_totals_flag_a_partially_priced_session(
    repo: Repository, session_row: Session, run_row: Run, prices: PriceTable
) -> None:
    priced = await repo.append_usage(run_row.id, usage(), prices=prices)
    await repo.append_usage(run_row.id, usage(model=UNPRICED_MODEL), prices=prices)

    totals = await repo.usage_totals(session_id=session_row.id)
    assert totals.events == 2
    assert totals.unpriced_events == 1
    assert totals.complete is False
    # SUM() skips the NULL, so the number is real but incomplete — which is
    # exactly what `complete` exists to say out loud.
    assert totals.cost_usd == priced.cost_usd


async def test_totals_are_empty_but_not_wrong_without_usage(
    repo: Repository, session_row: Session
) -> None:
    totals = await repo.usage_totals(session_id=session_row.id)
    assert totals.counts.total == 0
    assert totals.events == 0
    assert totals.cost_usd is None
    assert totals.complete is True


async def test_totals_can_be_scoped_to_a_node(
    repo: Repository, node_row: Node, run_row: Run, prices: PriceTable
) -> None:
    await repo.append_usage(run_row.id, usage(), prices=prices)
    assert (await repo.usage_totals(node_id=node_row.id)).events == 1
    assert (await repo.usage_totals(node_id=new_node_id())).events == 0


async def test_usage_rows_are_ordered_and_numbered_within_a_run(
    repo: Repository, run_row: Run, prices: PriceTable
) -> None:
    for _ in range(3):
        await repo.append_usage(run_row.id, usage(), prices=prices)

    assert [row.seq for row in await repo.list_usage(run_row.id)] == [0, 1, 2]


async def test_the_same_usage_cannot_be_ingested_twice(
    repo: Repository, run_row: Run, prices: PriceTable
) -> None:
    """``uq_usage_event_run_id_seq`` turns a double ingest into an error.

    Without it, a crash between the NDJSON append and the projection would let a
    retrying ingest double every token count, and nothing would notice.
    """
    await repo.append_usage(run_row.id, usage(), prices=prices, seq=0)
    with pytest.raises(IntegrityError):
        await repo.append_usage(run_row.id, usage(), prices=prices, seq=0)


async def test_usage_for_an_unknown_run_is_a_programmer_error(
    repo: Repository, prices: PriceTable
) -> None:
    with pytest.raises(RepositoryError, match="no such run"):
        await repo.append_usage(new_run_id(), usage(), prices=prices)


async def test_usage_event_is_append_only(
    repo: Repository, database: Database, run_row: Run, prices: PriceTable
) -> None:
    """No update path in the API, and none in the database either."""
    assert not [
        name
        for name in dir(Repository)
        if "usage" in name and ("update" in name or "delete" in name)
    ]

    await repo.append_usage(run_row.id, usage(), prices=prices)

    async with database.engine.begin() as connection:
        with pytest.raises((IntegrityError, OperationalError), match="append-only"):
            await connection.execute(sa.text("UPDATE usage_event SET input_tokens = 0"))

    rows = await repo.list_usage(run_row.id)
    assert rows[0].input_tokens == 1_200


async def test_discarding_a_run_takes_its_usage_with_it(
    repo: Repository,
    session_row: Session,
    node_row: Node,
    run_row: Run,
    prices: PriceTable,
) -> None:
    """How B3 rebuilds: drop the whole run projection, then re-ingest the log.

    The node and the session above it survive — they carry authored input that
    no event log can reproduce.
    """
    await repo.append_usage(run_row.id, usage(), prices=prices)

    assert await repo.delete_run(run_row.id) is True
    assert await repo.get_run(run_row.id) is None
    assert (await repo.usage_totals(session_id=session_row.id)).events == 0
    assert await repo.get_node(node_row.id) is not None
    assert await repo.get_session(session_row.id) is not None

    assert await repo.delete_run(run_row.id) is False


async def test_a_rebuilt_run_can_reuse_its_identity(
    repo: Repository, node_row: Node, run_row: Run, prices: PriceTable
) -> None:
    """Replay re-creates the same run id, attempt and rows from the same log."""
    await repo.append_usage(run_row.id, usage(), prices=prices)
    await repo.delete_run(run_row.id)

    rebuilt = await repo.create_run(
        run_id=run_row.id,
        node_id=node_row.id,
        events_path=run_row.events_path,
        attempt=run_row.attempt,
    )
    await repo.append_usage(rebuilt.id, usage(), prices=prices, seq=0)

    assert rebuilt.attempt == run_row.attempt
    assert [row.seq for row in await repo.list_usage(rebuilt.id)] == [0]


async def test_deleting_a_session_cascades_to_runs_and_usage(
    repo: Repository,
    database: Database,
    session_row: Session,
    run_row: Run,
    prices: PriceTable,
) -> None:
    await repo.append_usage(run_row.id, usage(), prices=prices)

    stored = await repo.get_session(session_row.id)
    assert stored is not None
    await repo.session.delete(stored)
    await repo.session.commit()

    async with database.engine.connect() as connection:
        counts = {
            table: (
                await connection.execute(sa.text(f"SELECT count(*) FROM {table}"))
            ).scalar()
            for table in ("node", "run", "usage_event")
        }
    assert counts == {"node": 0, "run": 0, "usage_event": 0}


async def test_sessions_are_listed_newest_first(repo: Repository) -> None:
    ids = []
    for index in range(3):
        row = await repo.create_session(
            session_id=f"sess_{index:03d}",
            title=f"session {index}",
            repo_path=Path("/tmp/target-repo"),
            workspace_root=Path("/tmp/workspaces"),
            integration_branch="agenthub/x/integration",
        )
        ids.append(row.id)

    # ULIDs sort by creation time, so ORDER BY id is chronological.
    assert [row.id for row in await repo.list_sessions()] == list(reversed(ids))
    assert [row.id for row in await repo.list_sessions(limit=1)] == [ids[-1]]


async def test_concurrent_writers_do_not_lose_rows(
    database: Database, run_row: Run, prices: PriceTable
) -> None:
    """WAL allows one writer at a time; ``busy_timeout`` makes the others wait.

    Each task gets its own session and its own ``seq``: an automatically
    allocated sequence would race, and the unique constraint would (correctly)
    turn that race into an error rather than a duplicated total.
    """

    async def writer(seq: int) -> None:
        async with repository(database) as writer_repo:
            await writer_repo.append_usage(
                run_row.id, usage(ts=1_700_000_000_000 + seq), prices=prices, seq=seq
            )

    await asyncio.gather(*(writer(seq) for seq in range(8)))

    async with repository(database) as reader:
        stored = await reader.list_usage(run_row.id)
        totals = await reader.usage_totals(run_id=run_row.id)

    assert [row.seq for row in stored] == list(range(8))
    assert totals.events == 8
    assert totals.counts.input_tokens == 8 * 1_200
