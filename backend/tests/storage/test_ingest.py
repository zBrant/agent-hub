"""Tests for the ordered write path (`docs/architecture.md` §4).

NDJSON → SQLite → broadcast. The tests below check the order itself, not just
the end state: an implementation that projects first and appends second passes
every "is the row correct" assertion and still destroys replay.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.harnesses.base import ParseStats
from app.harnesses.events import (
    AgentEvent,
    RawChunk,
    RunFinished,
    RunStarted,
    Usage,
)
from app.models.pricing import PriceTable
from app.models.status import RunState, UsageSource
from app.models.tables import Run
from app.storage.ingest import IngestError, Projection, ingest_run
from app.storage.meta import RunMeta, read_meta_sync
from app.storage.ndjson import events_path, read_events
from app.storage.repository import Repository


def log_lines(path: Path) -> int:
    return 0 if not path.exists() else len(path.read_text(encoding="utf-8").split("\n"))


# --------------------------------------------------------------------------
# The order
# --------------------------------------------------------------------------


async def test_the_event_is_durable_before_it_is_projected(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    """Step 3 can only observe a world where steps 1 and 2 already happened."""
    path = events_path(runs_root, run_meta.run_id)
    seen: list[tuple[str, int, str]] = []

    async def observe(event: AgentEvent) -> None:
        row = await repo.get_run(run_meta.run_id)
        assert row is not None
        # Lines on disk (the trailing newline makes an extra split entry) and
        # the projected status, sampled at broadcast time.
        seen.append((event.type, log_lines(path) - 1, row.status.value))

    async with ingest_run(
        repository=repo,
        runs_root=runs_root,
        meta=run_meta,
        prices=prices,
        broadcast=observe,
    ) as ingest:
        for event in event_stream:
            await ingest.ingest(event)

    # Every broadcast saw its own event already on disk.
    assert [lines for _, lines, _ in seen] == list(range(1, len(seen) + 1))
    # ...and the projection of the terminal event already applied.
    assert seen[-1] == ("run_finished", 11, "success")


async def test_a_failed_projection_still_leaves_the_fact_on_disk(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Death between steps 1 and 2 is the case replay exists for.

    The log must already contain the event, so a rebuild recovers it. Reverse
    the two steps and this is a row the log cannot explain.
    """

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("sqlite went away")

    monkeypatch.setattr(repo, "start_run", explode)
    events = event_stream

    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ) as ingest:
        with pytest.raises(RuntimeError):
            await ingest.ingest(events[0])

    assert list(read_events(events_path(runs_root, run_meta.run_id))) == [events[0]]


async def test_a_failed_broadcast_does_not_undo_the_projection(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    """Death between steps 2 and 3 costs a frame, never a row."""

    async def explode(event: AgentEvent) -> None:
        raise RuntimeError("nobody is listening")

    events = event_stream
    async with ingest_run(
        repository=repo,
        runs_root=runs_root,
        meta=run_meta,
        prices=prices,
        broadcast=explode,
    ) as ingest:
        with pytest.raises(RuntimeError):
            await ingest.ingest(events[0])

    row = await repo.get_run(run_meta.run_id)
    assert row is not None and row.pid == 4242


async def test_meta_is_written_before_the_first_event(
    repo: Repository, runs_root: Path, run_meta: RunMeta, prices: PriceTable
) -> None:
    """Without it a crashed run cannot be relinked to its node."""
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ):
        on_disk = read_meta_sync(runs_root / run_meta.run_id / "meta.json")
        assert on_disk.node_id == run_meta.node_id
        assert on_disk.finalized is False
        assert on_disk.trusted is False


async def test_finalize_makes_the_parser_verdict_durable(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ) as ingest:
        for event in event_stream:
            await ingest.ingest(event)
        await ingest.finalize(at_ms=2_000, stats=ParseStats(lines=11, events=11))

    on_disk = read_meta_sync(runs_root / run_meta.run_id / "meta.json")
    assert on_disk.finalized_ms == 2_000
    assert on_disk.trusted is True


# --------------------------------------------------------------------------
# The projection
# --------------------------------------------------------------------------


async def test_a_whole_run_projects_the_derived_columns(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ) as ingest:
        for event in event_stream:
            await ingest.ingest(event)

    row = await repo.get_run(run_meta.run_id)
    assert row is not None
    assert row.status is RunState.SUCCESS
    assert row.harness_session_id == "thread-abc"
    assert row.harness_version == "0.101.0"
    assert row.started_ms == 1_000
    assert row.finished_ms == 1_100
    assert row.exit_code == 0
    # Every line of the log, including run_finished itself.
    assert row.event_count == 11
    assert row.permission_denial_count == 1


async def test_usage_seq_is_dense_and_starts_at_zero(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    """``seq`` is what makes a rebuild the same rows rather than similar ones."""
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ) as ingest:
        for event in event_stream:
            await ingest.ingest(event)

    rows = await repo.list_usage(run_meta.run_id)
    assert [row.seq for row in rows] == [0, 1]
    assert rows[1].source is UsageSource.RECONSTRUCTED


async def test_cost_is_computed_at_ingest_with_the_pinned_version(
    repo: Repository,
    runs_root: Path,
    run_meta: RunMeta,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    async with ingest_run(
        repository=repo, runs_root=runs_root, meta=run_meta, prices=prices
    ) as ingest:
        for event in event_stream:
            await ingest.ingest(event)

    rows = await repo.list_usage(run_meta.run_id)
    assert {row.price_table_version for row in rows} == {prices.version}
    assert all(row.cost_usd is not None for row in rows)


async def test_two_identical_usage_events_are_two_rows(
    repo: Repository, logged_run: Run, prices: PriceTable, model: str
) -> None:
    """Identical in every field but ``seq``; collapsing them halves the bill."""
    projection = Projection(repo, logged_run.id, prices=prices)
    usage = Usage(run_id=logged_run.id, ts=5, model=model, input_tokens=100)
    await projection.apply(usage)
    await projection.apply(usage)

    rows = await repo.list_usage(logged_run.id)
    assert [row.seq for row in rows] == [0, 1]
    totals = await repo.usage_totals(run_id=logged_run.id)
    assert totals.counts.input_tokens == 200


async def test_narrative_events_are_not_projected(
    repo: Repository,
    logged_run: Run,
    prices: PriceTable,
    event_stream: list[AgentEvent],
) -> None:
    projection = Projection(repo, logged_run.id, prices=prices)
    for event in event_stream:
        if isinstance(event, RunStarted | Usage | RunFinished):
            continue
        await projection.apply(event)

    assert projection.events == 7
    assert projection.usage_events == 0
    assert await repo.list_usage(logged_run.id) == []


# --------------------------------------------------------------------------
# Refusals — programmer error, not agent failure
# --------------------------------------------------------------------------


async def test_an_event_from_another_run_is_refused(
    repo: Repository, logged_run: Run, prices: PriceTable, model: str
) -> None:
    projection = Projection(repo, logged_run.id, prices=prices)
    with pytest.raises(IngestError, match="applied to run"):
        await projection.apply(
            Usage(run_id="run_somebody_else", ts=1, model=model, input_tokens=1)
        )


async def test_a_pty_chunk_is_refused_by_the_structural_log(
    repo: Repository, logged_run: Run, prices: PriceTable
) -> None:
    """Channel B goes to pty.log; it is pixels, and it is unbounded."""
    projection = Projection(repo, logged_run.id, prices=prices)
    with pytest.raises(IngestError, match=r"pty\.log"):
        await projection.apply(RawChunk(run_id=logged_run.id, ts=1, data=b"\x1b[2J"))


async def test_a_price_table_that_is_not_the_pinned_one_is_refused(
    repo: Repository, runs_root: Path, run_meta: RunMeta, prices: PriceTable
) -> None:
    """Pricing a run under a table it does not claim is undetectable later."""
    other = PriceTable(
        version=prices.version + 1,
        models=prices.models,
        cache_write_multiplier_5m=1.25,
        cache_write_multiplier_1h=2.0,
        cache_read_multiplier=0.1,
    )
    with pytest.raises(IngestError, match="pins price table version"):
        async with ingest_run(
            repository=repo, runs_root=runs_root, meta=run_meta, prices=other
        ):
            pass
