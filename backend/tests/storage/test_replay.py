"""Tests for ``agenthub replay`` — invariant 4, stated as assertions.

The claim under test is that SQLite is *derived*: throw the run's rows away,
rebuild them from ``events.ndjson``, and get the same rows back. If any test
here fails, the projection has taken on responsibility the log cannot cover.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.harnesses.events import AgentEvent, RunFinished, Usage
from app.models.pricing import (
    ModelPrice,
    PriceHistory,
    PriceTable,
    PriceTableNotFound,
    TokenCounts,
)
from app.models.status import NodeStatus, RunState
from app.models.tables import Node, Run, Session
from app.storage.ingest import ingest_run
from app.storage.meta import MetaError, RunMeta, meta_path, write_meta_sync
from app.storage.ndjson import EventLog, events_path, write_events_sync
from app.storage.replay import ReplayError, read_log, replay_run
from app.storage.repository import Repository

# Fields of `run` that a rebuild must reproduce exactly. `id` is the primary
# key; everything else is either authored input carried in meta.json or derived
# from a line of the log.
RUN_FIELDS = (
    "id",
    "node_id",
    "session_id",
    "attempt",
    "status",
    "harness",
    "model",
    "cwd",
    "pid",
    "harness_session_id",
    "harness_version",
    "events_path",
    "started_ms",
    "finished_ms",
    "exit_code",
    "summary",
    "event_count",
    "permission_denial_count",
    "created_ms",
)

# `id` is an autoincrement surrogate and is expected to change; everything a
# dashboard reads must not.
USAGE_FIELDS = (
    "run_id",
    "node_id",
    "session_id",
    "seq",
    "ts",
    "harness",
    "model",
    "source",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "price_table_version",
    "cost_usd",
)


def snapshot(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(row, name) for name in fields}


async def run_state(repo: Repository, run_id: str) -> dict[str, Any]:
    run = await repo.get_run(run_id)
    assert run is not None
    return {
        "run": snapshot(run, RUN_FIELDS),
        "usage": [snapshot(row, USAGE_FIELDS) for row in await repo.list_usage(run_id)],
        "totals": await repo.usage_totals(run_id=run_id),
    }


async def live_ingest(
    repo: Repository,
    runs_root: Path,
    meta: RunMeta,
    prices: PriceTable,
    events: list[AgentEvent],
) -> None:
    """What B4 will do: the ordered write path, start to finish."""
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=meta, prices=prices
    ) as ingest:
        for event in events:
            await ingest.ingest(event)


def write_log_only(runs_root: Path, meta: RunMeta, events: list[AgentEvent]) -> None:
    """Death after step 1: the log is on disk, SQLite never heard about it."""
    write_meta_sync(meta_path(runs_root, meta.run_id), meta)
    write_events_sync(events_path(runs_root, meta.run_id), events)


# --------------------------------------------------------------------------
# The rebuild reproduces the original
# --------------------------------------------------------------------------


async def test_a_rebuilt_run_equals_the_one_live_ingest_produced(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """The assertion that makes invariant 4 true rather than aspirational."""
    await live_ingest(repo, runs_root, run_meta, prices, event_stream)
    before = await run_state(repo, run_meta.run_id)

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert await run_state(repo, run_meta.run_id) == before
    assert result.events == 11
    assert result.usage_events == 2
    assert result.run_status is RunState.SUCCESS
    assert result.permission_denials == 1
    assert result.truncated is False


async def test_replaying_twice_changes_nothing(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """Idempotent: same rows, same seq values, same totals.

    A rebuild that appended instead of replacing would double every total, and
    ``uq_usage_event_run_id_seq`` exists so that failure is loud rather than
    quiet.
    """
    await live_ingest(repo, runs_root, run_meta, prices, event_stream)

    async def once() -> dict[str, Any]:
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=price_history,
        )
        return await run_state(repo, run_meta.run_id)

    assert await once() == await once()


async def test_timestamps_come_from_the_events_not_from_the_replay(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """B2 made ``at_ms`` explicit for exactly this."""
    write_log_only(runs_root, run_meta, event_stream)
    await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    run = await repo.get_run(run_meta.run_id)
    assert run is not None
    assert run.created_ms == run_meta.created_ms
    assert run.started_ms == 1_000
    assert run.finished_ms == 1_100
    assert [row.ts for row in await repo.list_usage(run_meta.run_id)] == [1_050, 1_080]


# --------------------------------------------------------------------------
# Crash boundaries
# --------------------------------------------------------------------------


async def test_death_after_step_one_is_fully_recovered(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """NDJSON written, SQLite never updated. This is why the order is fixed."""
    write_log_only(runs_root, run_meta, event_stream)

    stale = await repo.get_run(run_meta.run_id)
    assert stale is not None and stale.status is RunState.RUNNING
    assert await repo.list_usage(run_meta.run_id) == []

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert result.run_status is RunState.SUCCESS
    assert (
        result.totals.counts.total
        == TokenCounts(
            input_tokens=34,
            output_tokens=353,
            cache_read_tokens=22_737,
            cache_write_tokens=6_513,
        ).total
    )


async def test_a_run_the_database_never_heard_of_is_still_rebuildable(
    repo: Repository,
    runs_root: Path,
    node_row: Node,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """meta.json is what relinks the run to its node; the log cannot.

    ``RunStarted`` carries the *harness's* session id, so with the row deleted
    there is nothing in the stream pointing back up the graph.
    """
    write_log_only(runs_root, run_meta, event_stream)
    assert await repo.delete_run(run_meta.run_id) is True

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert result.node_id == node_row.id
    rebuilt = await repo.get_run(run_meta.run_id)
    assert rebuilt is not None
    assert rebuilt.attempt == run_meta.attempt
    assert rebuilt.status is RunState.SUCCESS


async def test_death_after_step_two_does_not_duplicate_anything(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """SQLite updated, broadcast never sent: replay must be a no-op in effect."""
    broadcasts: list[AgentEvent] = []

    async def dies_before_broadcasting(event: AgentEvent) -> None:
        if isinstance(event, RunFinished):
            raise RuntimeError("the process died before the frame went out")
        broadcasts.append(event)

    async with ingest_run(
        repository=repo,
        runs_root=runs_root,
        meta=run_meta,
        prices=prices,
        broadcast=dies_before_broadcasting,
    ) as ingest:
        with pytest.raises(RuntimeError):
            for event in event_stream:
                await ingest.ingest(event)

    before = await run_state(repo, run_meta.run_id)
    assert before["run"]["status"] is RunState.SUCCESS

    await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )
    assert await run_state(repo, run_meta.run_id) == before


async def test_a_log_with_no_terminal_event_replays_as_interrupted(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    write_log_only(runs_root, run_meta, event_stream[:-1])

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert result.run_status is RunState.INTERRUPTED
    run = await repo.get_run(run_meta.run_id)
    assert run is not None
    # The last event's stamp, not the moment of the rebuild.
    assert run.finished_ms == 1_090
    # The two usage events before the kill are still counted.
    assert result.usage_events == 2
    # Interrupted runs retain the same cheap replay/idempotency checks as runs
    # that emitted RunFinished.
    assert run.event_count == len(event_stream) - 1
    assert run.permission_denial_count == result.permission_denials == 1


# --------------------------------------------------------------------------
# A torn final line
# --------------------------------------------------------------------------


async def test_a_torn_final_line_recovers_everything_before_it(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """SIGKILL mid-write. Policy: keep the complete records, mark interrupted."""
    write_log_only(runs_root, run_meta, event_stream[:-1])
    path = events_path(runs_root, run_meta.run_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type":"run_finished","run_id":"run_01J","ts":1100,"sta')

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert result.truncated_line == 11
    assert result.events == 10
    assert result.run_status is RunState.INTERRUPTED
    run = await repo.get_run(run_meta.run_id)
    assert run is not None and run.summary is not None
    assert "truncated" in run.summary
    assert run.event_count == result.events == 10
    assert run.permission_denial_count == result.permission_denials == 1


async def test_a_torn_line_in_the_middle_is_corruption_and_refuses(
    tmp_path: Path, run_meta: RunMeta, event_stream: list[AgentEvent]
) -> None:
    """Only the tail can be torn: the writer flushes each line before the next."""
    path = tmp_path / "events.ndjson"
    write_events_sync(path, event_stream)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[4] = lines[4][:20]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ReplayError, match="corrupt"):
        read_log(path)


async def test_a_damaged_last_line_that_was_fully_written_is_corruption(
    tmp_path: Path, event_stream: list[AgentEvent]
) -> None:
    """A trailing newline means the record completed; something else broke it."""
    path = tmp_path / "events.ndjson"
    write_events_sync(path, event_stream)
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[-1] = lines[-1][:20]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(ReplayError, match="corrupt"):
        read_log(path)


async def test_a_replayed_log_re_serializes_to_the_same_bytes(
    runs_root: Path,
    run_meta: RunMeta,
    tmp_path: Path,
    event_stream: list[AgentEvent],
) -> None:
    """Byte-for-byte event compatible: the log survives a read/write cycle.

    One serialization feeds the log, the WebSocket frame and the replay payload
    (`docs/architecture.md` §2). If a round trip changed a single byte, one of
    those three consumers is seeing a different event from the other two.
    """
    original = events_path(runs_root, run_meta.run_id)
    write_events_sync(original, event_stream)

    rewritten = tmp_path / "again.ndjson"
    write_events_sync(rewritten, read_log(original).events)

    assert rewritten.read_bytes() == original.read_bytes()


async def test_an_empty_log_replays_as_an_interrupted_run_with_no_usage(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
) -> None:
    write_log_only(runs_root, run_meta, [])

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert result.events == 0
    assert result.run_status is RunState.INTERRUPTED
    run = await repo.get_run(run_meta.run_id)
    assert run is not None and run.finished_ms == run_meta.created_ms


async def test_a_flushed_line_with_no_trailing_newline_is_a_complete_event(
    repo: Repository, runs_root: Path, run_meta: RunMeta, event_stream: list[AgentEvent]
) -> None:
    """A complete record whose trailing newline never landed is not torn.

    ``EventLog`` writes the record and its separator as two calls, so the last
    bytes to reach the disk can be the record without the ``\\n``. The event
    parses, therefore it is a fact, therefore it is kept.
    """
    async with EventLog(events_path(runs_root, run_meta.run_id)) as log:
        for event in event_stream:
            await log.append(event)
    path = events_path(runs_root, run_meta.run_id)
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")

    recovered = read_log(path)
    assert recovered.truncated is False
    assert list(recovered.events) == event_stream


# --------------------------------------------------------------------------
# Price history: the rebuild must not reprice
# --------------------------------------------------------------------------


def older_and_dearer(prices: PriceTable) -> PriceHistory:
    """A history whose *current* table costs ten times the superseded one."""
    dearer = PriceTable(
        version=prices.version + 1,
        models={
            name: ModelPrice(input=price.input * 10, output=price.output * 10)
            for name, price in prices.models.items()
        },
        cache_write_multiplier_5m=prices.cache_write_multiplier_5m,
        cache_write_multiplier_1h=prices.cache_write_multiplier_1h,
        cache_read_multiplier=prices.cache_read_multiplier,
    )
    return PriceHistory(current=dearer, superseded={prices.version: prices})


async def test_replay_prices_with_the_pinned_version_not_the_current_one(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    """The correctness point of B3.

    A naive rebuild reaches for today's table and silently rewrites the cost of
    every historical run — invisible in any diff, and the exact failure
    invariant 3 exists to prevent.
    """
    await live_ingest(repo, runs_root, run_meta, prices, event_stream)
    original = (await repo.usage_totals(run_id=run_meta.run_id)).cost_usd
    assert original is not None

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=older_and_dearer(prices),
    )

    assert result.totals.cost_usd == pytest.approx(original)
    assert result.price_table_version == prices.version
    rows = await repo.list_usage(run_meta.run_id)
    assert {row.price_table_version for row in rows} == {prices.version}


async def test_replay_refuses_when_the_pinned_version_is_gone(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    """Refusing is recoverable; repricing is not."""
    await live_ingest(repo, runs_root, run_meta, prices, event_stream)
    before = await run_state(repo, run_meta.run_id)

    history = older_and_dearer(prices)
    forgetful = PriceHistory(current=history.current, source=Path("pricing.yaml"))

    with pytest.raises(PriceTableNotFound) as raised:
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=forgetful,
        )

    assert raised.value.version == prices.version
    assert f"version {prices.version}" in str(raised.value)
    # It refused *before* deleting anything: a half-rebuilt run is worse than
    # an unrebuilt one.
    assert await run_state(repo, run_meta.run_id) == before


# --------------------------------------------------------------------------
# What replay must never touch
# --------------------------------------------------------------------------


async def test_replay_never_deletes_authored_rows(
    repo: Repository,
    runs_root: Path,
    session_row: Session,
    node_row: Node,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """``session`` and ``node`` are input. No log can invent them back."""
    write_log_only(runs_root, run_meta, event_stream)
    await repo.set_node_status(node_row.id, NodeStatus.AWAITING_REVIEW, at_ms=7)

    await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    node = await repo.get_node(node_row.id)
    assert node is not None
    assert node.name == "main"
    assert node.prompt == "add a docstring to foo()"
    # The node transition is `orchestrator/graph.py:transition`'s call, not
    # storage's. Replay reports the run state and leaves the node alone.
    assert node.status is NodeStatus.AWAITING_REVIEW
    assert node.updated_ms == 7
    assert await repo.get_session(session_row.id) is not None


async def test_other_runs_of_the_same_node_are_untouched(
    repo: Repository,
    runs_root: Path,
    node_row: Node,
    run_row: Run,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """A retry is a separate run; rebuilding one must not disturb the other."""
    write_log_only(runs_root, run_meta, event_stream)
    other = snapshot(run_row, RUN_FIELDS)

    await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    kept = await repo.get_run(run_row.id)
    assert kept is not None
    assert snapshot(kept, RUN_FIELDS) == other
    assert {run.attempt for run in await repo.list_runs(node_row.id)} == {1, 2}


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


async def test_replay_refuses_a_run_with_no_metadata(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    write_events_sync(events_path(runs_root, run_meta.run_id), event_stream)

    with pytest.raises(MetaError, match="cannot read run metadata"):
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=price_history,
        )


async def test_replay_refuses_when_the_node_is_gone(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    orphaned = run_meta.model_copy(update={"node_id": "node_deleted"})
    write_log_only(runs_root, orphaned, event_stream)

    with pytest.raises(ReplayError, match="authored input"):
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=price_history,
        )


async def test_replay_refuses_metadata_for_a_different_run(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
) -> None:
    write_meta_sync(
        meta_path(runs_root, run_meta.run_id),
        run_meta.model_copy(update={"run_id": "run_somebody_else"}),
    )

    with pytest.raises(ReplayError, match="describes run"):
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=price_history,
        )


async def test_replay_refuses_a_node_from_another_session(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    write_log_only(
        runs_root,
        run_meta.model_copy(update={"session_id": "sess_other"}),
        event_stream,
    )

    with pytest.raises(ReplayError, match="belongs to"):
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=price_history,
        )


async def test_a_missing_log_refuses_rather_than_emptying_the_run(
    repo: Repository, runs_root: Path, run_meta: RunMeta, price_history: PriceHistory
) -> None:
    write_meta_sync(meta_path(runs_root, run_meta.run_id), run_meta)

    with pytest.raises(ReplayError, match="cannot read event log"):
        await replay_run(
            repository=repo,
            runs_root=runs_root,
            run_id=run_meta.run_id,
            prices=price_history,
        )


async def test_an_untrusted_run_replays_and_says_so(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    """B7 refuses to merge on this; the flag has to survive the rebuild."""
    write_log_only(runs_root, run_meta, event_stream)

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )
    assert result.trusted is False


async def test_an_unpriced_model_replays_as_unknown_not_zero(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    price_history: PriceHistory,
    event_stream: list[AgentEvent],
) -> None:
    unknown_model = [
        event.model_copy(update={"model": "some-future-model"})
        if isinstance(event, Usage)
        else event
        for event in event_stream
    ]
    write_log_only(runs_root, run_meta, unknown_model)

    result = await replay_run(
        repository=repo,
        runs_root=runs_root,
        run_id=run_meta.run_id,
        prices=price_history,
    )

    assert result.totals.cost_usd is None
    assert result.totals.complete is False
