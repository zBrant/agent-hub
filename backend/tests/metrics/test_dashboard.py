"""D1 dashboard aggregation over the durable SQLite projection."""

from pathlib import Path

from app.harnesses.events import Usage
from app.metrics.dashboard import DashboardPeriod, DashboardService
from app.models.clock import now_ms
from app.models.ids import new_node_id, new_run_id, new_session_id
from app.models.pricing import load_price_table
from app.models.status import NodeStatus, SessionStatus
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"


async def test_snapshot_groups_four_field_usage_and_active_progress(
    tmp_path: Path,
) -> None:
    root = tmp_path / "agenthub"
    database_url = f"sqlite+aiosqlite:///{root / 'agenthub.db'}"
    upgrade_database_sync(database_url)
    database = Database(database_url)
    prices = load_price_table(PRICING_YAML)
    stamp = now_ms()

    try:
        async with database.session() as db_session:
            repo = Repository(db_session)
            active_id = new_session_id()
            await repo.create_session(
                session_id=active_id,
                title="Active graph",
                repo_path=Path("/repo/active"),
                workspace_root=root / "workspaces" / active_id,
                integration_branch=f"agenthub/{active_id}/integration",
                status=SessionStatus.PAUSED,
                at_ms=stamp - 10_000,
            )
            done_node = await repo.create_node(
                node_id=new_node_id(),
                session_id=active_id,
                name="done",
                prompt="done",
                harness="codex",
                model="gpt-5.6-terra",
                status=NodeStatus.DONE,
                at_ms=stamp - 9_000,
            )
            await repo.create_node(
                node_id=new_node_id(),
                session_id=active_id,
                name="blocked",
                prompt="blocked",
                harness="claude-code",
                model="claude-sonnet-4-5",
                status=NodeStatus.BLOCKED,
                at_ms=stamp - 8_000,
            )
            active_run_id = new_run_id()
            await repo.create_run(
                run_id=active_run_id,
                node_id=done_node.id,
                events_path=root / "runs" / active_run_id / "events.ndjson",
            )
            await repo.append_usage(
                active_run_id,
                Usage(
                    run_id=active_run_id,
                    ts=stamp - 7_000,
                    model="gpt-5.6-terra",
                    input_tokens=1,
                    output_tokens=2,
                    cache_read_tokens=3,
                    cache_write_tokens=4,
                ),
                prices=prices,
            )

            finished_id = new_session_id()
            await repo.create_session(
                session_id=finished_id,
                title="Finished graph",
                repo_path=Path("/repo/finished"),
                workspace_root=root / "workspaces" / finished_id,
                integration_branch=f"agenthub/{finished_id}/integration",
                status=SessionStatus.DONE,
                at_ms=stamp - 6_000,
            )
            failed_node = await repo.create_node(
                node_id=new_node_id(),
                session_id=finished_id,
                name="failed",
                prompt="failed",
                harness="codex",
                model="unpriced-model",
                status=NodeStatus.FAILED,
                at_ms=stamp - 5_000,
            )
            failed_run_id = new_run_id()
            await repo.create_run(
                run_id=failed_run_id,
                node_id=failed_node.id,
                events_path=root / "runs" / failed_run_id / "events.ndjson",
            )
            await repo.append_usage(
                failed_run_id,
                Usage(
                    run_id=failed_run_id,
                    ts=stamp - 4_000,
                    model="unpriced-model",
                    input_tokens=10,
                    output_tokens=20,
                    cache_read_tokens=30,
                    cache_write_tokens=40,
                ),
                prices=prices,
            )

        snapshot = await DashboardService(database).snapshot(DashboardPeriod.SEVEN_DAYS)
    finally:
        await database.dispose()

    assert snapshot.period is DashboardPeriod.SEVEN_DAYS
    assert snapshot.usage.counts.input_tokens == 11
    assert snapshot.usage.counts.output_tokens == 22
    assert snapshot.usage.counts.cache_read_tokens == 33
    assert snapshot.usage.counts.cache_write_tokens == 44
    assert snapshot.usage.counts.total == 110
    assert snapshot.usage.cost_usd is not None
    assert snapshot.usage.cost_complete is False
    assert [(row.key, row.counts.total) for row in snapshot.by_harness] == [
        ("codex", 110)
    ]
    assert [(row.key, row.counts.total) for row in snapshot.by_model] == [
        ("gpt-5.6-terra", 10),
        ("unpriced-model", 100),
    ]
    assert snapshot.active_session_count == 1
    assert snapshot.running_node_count == 0
    assert snapshot.blocked_node_count == 1
    assert snapshot.node_completion_rate == 0.5

    active = snapshot.active_sessions[0]
    assert active.id == active_id
    assert active.total_nodes == 2
    assert active.completed_nodes == 1
    assert active.blocked_nodes == 1
    assert active.harnesses == ("claude-code", "codex")
    assert active.usage.counts.total == 10
    assert active.elapsed_ms >= 10_000


def test_period_boundaries_are_utc_and_rolling() -> None:
    end = 40 * 86_400_000 + 12_345
    assert DashboardPeriod.TODAY.since_ms(end) == 40 * 86_400_000
    assert DashboardPeriod.SEVEN_DAYS.since_ms(end) == end - 7 * 86_400_000
    assert DashboardPeriod.THIRTY_DAYS.since_ms(end) == end - 30 * 86_400_000
