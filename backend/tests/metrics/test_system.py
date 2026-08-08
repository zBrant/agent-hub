"""D3 bounded system sampler and process-tree accounting."""

from __future__ import annotations

import asyncio
import threading
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.harnesses.events import RunStarted
from app.metrics.system import (
    AgentProcessMetric,
    ProcessCandidate,
    SystemMinuteWriter,
    SystemProbe,
    SystemSampler,
    SystemSnapshot,
)
from app.models.ids import new_node_id, new_run_id, new_session_id
from app.models.tables import SystemMetricMinute
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository


class MissingProcess(Exception):
    pass


class DeniedProcess(Exception):
    pass


class FakeProcess:
    def __init__(
        self,
        *,
        created: float = 5.0,
        rss: int = 0,
        cpu: float = 0,
        children: tuple[FakeProcess, ...] = (),
        vanish: bool = False,
    ) -> None:
        self.created = created
        self.rss = rss
        self.cpu = cpu
        self.descendants = children
        self.vanish = vanish

    def children(self, *, recursive: bool) -> list[FakeProcess]:
        assert recursive is True
        return list(self.descendants)

    def oneshot(self) -> Any:
        return nullcontext()

    def create_time(self) -> float:
        return self.created

    def memory_info(self) -> Any:
        if self.vanish:
            raise MissingProcess
        return SimpleNamespace(rss=self.rss)

    def cpu_percent(self, *, interval: None) -> float:
        assert interval is None
        return self.cpu


class FakePsutil:
    NoSuchProcess = MissingProcess
    AccessDenied = DeniedProcess

    def __init__(self, processes: dict[int, FakeProcess]) -> None:
        self.processes = processes

    def cpu_percent(self, *, interval: None, percpu: bool = False) -> Any:
        assert interval is None
        return [11.0, 22.0] if percpu else 16.5

    def virtual_memory(self) -> Any:
        return SimpleNamespace(total=1000, used=600, available=400, percent=60.0)

    def swap_memory(self) -> Any:
        return SimpleNamespace(total=200, used=50, free=150, percent=25.0)

    def disk_usage(self, path: str) -> Any:
        assert path == "/runtime"
        return SimpleNamespace(total=2000, used=750, free=1250, percent=37.5)

    def Process(self, pid: int) -> FakeProcess:
        try:
            return self.processes[pid]
        except KeyError as error:
            raise MissingProcess from error


def test_probe_sums_live_process_tree_and_ignores_disappearance() -> None:
    vanished = FakeProcess(vanish=True)
    child = FakeProcess(rss=50, cpu=2.5)
    root = FakeProcess(rss=100, cpu=1.5, children=(child, vanished))
    probe = SystemProbe(FakePsutil({42: root}))

    snapshot = probe.sample(
        ts=10_000,
        disk_path=Path("/runtime"),
        candidates=(
            ProcessCandidate(node_id="node_a", pid=42, harness="codex"),
            ProcessCandidate(node_id="node_gone", pid=99, harness="codex"),
        ),
    )

    assert snapshot.cpu_percent == 16.5
    assert snapshot.cpu_per_core == (11.0, 22.0)
    assert snapshot.memory_used_bytes == 600
    assert snapshot.swap_used_bytes == 50
    assert snapshot.disk_used_bytes == 750
    assert len(snapshot.processes) == 1
    process = snapshot.processes[0]
    assert process.node_id == "node_a"
    assert process.rss_bytes == 150
    assert process.cpu_percent == 4.0
    assert process.uptime_ms == 5_000
    assert process.process_count == 2


class ThreadProbe:
    def __init__(self) -> None:
        self.thread_ids: list[int] = []
        self.candidate_batches: list[tuple[ProcessCandidate, ...]] = []
        self.count = 0

    def sample(
        self,
        *,
        ts: int,
        disk_path: Path,
        candidates: tuple[ProcessCandidate, ...],
    ) -> SystemSnapshot:
        del ts, disk_path
        self.thread_ids.append(threading.get_ident())
        self.candidate_batches.append(candidates)
        self.count += 1
        return SystemSnapshot(
            ts=self.count,
            cpu_percent=0,
            cpu_per_core=(),
            memory_total_bytes=0,
            memory_used_bytes=0,
            memory_available_bytes=0,
            memory_percent=0,
            swap_total_bytes=0,
            swap_used_bytes=0,
            swap_free_bytes=0,
            swap_percent=0,
            disk_total_bytes=0,
            disk_used_bytes=0,
            disk_free_bytes=0,
            disk_percent=0,
            processes=(),
        )


def minute_snapshot(ts: int, cpu: float) -> SystemSnapshot:
    return SystemSnapshot(
        ts=ts,
        cpu_percent=cpu,
        cpu_per_core=(cpu,),
        memory_total_bytes=1_000,
        memory_used_bytes=500,
        memory_available_bytes=500,
        memory_percent=cpu + 1,
        swap_total_bytes=100,
        swap_used_bytes=10,
        swap_free_bytes=90,
        swap_percent=cpu + 2,
        disk_total_bytes=2_000,
        disk_used_bytes=500,
        disk_free_bytes=1_500,
        disk_percent=cpu + 3,
        processes=(
            AgentProcessMetric(
                node_id="node_live",
                pid=42,
                harness="codex",
                rss_bytes=int(cpu * 100),
                cpu_percent=cpu / 2,
                uptime_ms=ts,
                process_count=2,
            ),
        ),
    )


async def test_minute_writer_merges_partial_bucket_across_restart(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'agenthub.db'}"
    upgrade_database_sync(database_url)
    database = Database(database_url)

    first = SystemMinuteWriter(database)
    second = SystemMinuteWriter(database)
    try:
        await first.observe(minute_snapshot(1_000, 10))
        await first.observe(minute_snapshot(2_000, 30))
        # Crossing the boundary persists the completed first minute.
        await first.observe(minute_snapshot(61_000, 40))
        await first.close()

        # A restarted process contributes to the already persisted minute.
        await second.observe(minute_snapshot(62_000, 60))
        await second.close()

        async with database.session() as db_session:
            minute_zero = await db_session.get(SystemMetricMinute, 0)
            minute_one = await db_session.get(SystemMetricMinute, 60_000)
    finally:
        await first.close()
        await second.close()
        await database.dispose()

    assert minute_zero is not None
    assert minute_zero.sample_count == 2
    assert minute_zero.cpu_avg_percent == 20
    assert minute_zero.cpu_peak_percent == 30
    assert minute_zero.agent_rss_avg_bytes == 2_000
    assert minute_zero.agent_rss_peak_bytes == 3_000
    assert minute_zero.agent_process_count_peak == 2

    assert minute_one is not None
    assert minute_one.sample_count == 2
    assert minute_one.cpu_avg_percent == 50
    assert minute_one.cpu_peak_percent == 60
    assert minute_one.memory_avg_percent == 51
    assert minute_one.disk_peak_percent == 63


async def test_sampler_runs_off_loop_evicts_ring_and_cancels(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'agenthub.db'}"
    upgrade_database_sync(database_url)
    database = Database(database_url)
    probe = ThreadProbe()
    published: list[SystemSnapshot] = []

    async def publish(snapshot: SystemSnapshot) -> None:
        published.append(snapshot)

    async with database.session() as db_session:
        repo = Repository(db_session)
        session_id = new_session_id()
        await repo.create_session(
            session_id=session_id,
            title="running",
            repo_path=tmp_path,
            workspace_root=tmp_path / "workspaces" / session_id,
            integration_branch=f"agenthub/{session_id}/integration",
        )
        node = await repo.create_node(
            node_id=new_node_id(),
            session_id=session_id,
            name="running",
            prompt="running",
            harness="codex",
            model="gpt-5.6-terra",
        )
        run_id = new_run_id()
        await repo.create_run(
            run_id=run_id,
            node_id=node.id,
            events_path=tmp_path / "runs" / run_id / "events.ndjson",
        )
        await repo.start_run(
            run_id,
            RunStarted(
                run_id=run_id,
                ts=1,
                harness="codex",
                model="gpt-5.6-terra",
                cwd=tmp_path,
                pid=4_242,
            ),
        )
    sampler = SystemSampler(
        database=database,
        disk_path=tmp_path,
        interval_s=10,
        capacity=2,
        probe=probe,  # type: ignore[arg-type] - deliberately structural fake
        publish=publish,
    )
    loop_thread = threading.get_ident()
    try:
        await sampler.sample_once()
        await sampler.sample_once()
        await sampler.sample_once()
        assert [row.ts for row in sampler.history] == [2, 3]
        assert [row.ts for row in published] == [1, 2, 3]
        assert all(thread_id != loop_thread for thread_id in probe.thread_ids)
        assert probe.candidate_batches[0] == (
            ProcessCandidate(node_id=node.id, pid=4_242, harness="codex"),
        )

        assert sampler.start() is True
        assert sampler.start() is False
        await asyncio.sleep(0.05)
        assert sampler.running is True
        await sampler.close()
        assert sampler.running is False
    finally:
        await sampler.close()
        await database.dispose()
