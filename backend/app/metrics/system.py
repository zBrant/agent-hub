"""Bounded local system telemetry sampled off the event loop."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import structlog
from sqlmodel import col, select

from app.models.clock import now_ms
from app.models.status import RunState
from app.models.tables import Run, SystemMetricMinute
from app.storage.db import Database

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ProcessCandidate:
    node_id: str
    pid: int
    harness: str


@dataclass(frozen=True, slots=True)
class AgentProcessMetric:
    node_id: str
    pid: int
    harness: str
    rss_bytes: int
    cpu_percent: float
    uptime_ms: int
    process_count: int


@dataclass(frozen=True, slots=True)
class SystemSnapshot:
    ts: int
    cpu_percent: float
    cpu_per_core: tuple[float, ...]
    memory_total_bytes: int
    memory_used_bytes: int
    memory_available_bytes: int
    memory_percent: float
    swap_total_bytes: int
    swap_used_bytes: int
    swap_free_bytes: int
    swap_percent: float
    disk_total_bytes: int
    disk_used_bytes: int
    disk_free_bytes: int
    disk_percent: float
    processes: tuple[AgentProcessMetric, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "ts": self.ts,
            "cpu_percent": self.cpu_percent,
            "cpu_per_core": list(self.cpu_per_core),
            "memory_total_bytes": self.memory_total_bytes,
            "memory_used_bytes": self.memory_used_bytes,
            "memory_available_bytes": self.memory_available_bytes,
            "memory_percent": self.memory_percent,
            "swap_total_bytes": self.swap_total_bytes,
            "swap_used_bytes": self.swap_used_bytes,
            "swap_free_bytes": self.swap_free_bytes,
            "swap_percent": self.swap_percent,
            "disk_total_bytes": self.disk_total_bytes,
            "disk_used_bytes": self.disk_used_bytes,
            "disk_free_bytes": self.disk_free_bytes,
            "disk_percent": self.disk_percent,
            "processes": [
                {
                    "node_id": process.node_id,
                    "pid": process.pid,
                    "harness": process.harness,
                    "rss_bytes": process.rss_bytes,
                    "cpu_percent": process.cpu_percent,
                    "uptime_ms": process.uptime_ms,
                    "process_count": process.process_count,
                }
                for process in self.processes
            ],
        }


async def _discard_snapshot(_: SystemSnapshot) -> None:
    return None


MINUTE_MS = 60_000


@dataclass(slots=True)
class _MinuteAccumulator:
    minute_ms: int
    sample_count: int = 0
    cpu_sum: float = 0
    cpu_peak: float = 0
    memory_sum: float = 0
    memory_peak: float = 0
    swap_sum: float = 0
    swap_peak: float = 0
    disk_sum: float = 0
    disk_peak: float = 0
    agent_rss_sum: int = 0
    agent_rss_peak: int = 0
    agent_cpu_sum: float = 0
    agent_cpu_peak: float = 0
    agent_process_count_peak: int = 0

    @classmethod
    def from_snapshot(cls, snapshot: SystemSnapshot) -> _MinuteAccumulator:
        accumulator = cls(minute_ms=snapshot.ts - snapshot.ts % MINUTE_MS)
        accumulator.add(snapshot)
        return accumulator

    def add(self, snapshot: SystemSnapshot) -> None:
        agent_rss = sum(process.rss_bytes for process in snapshot.processes)
        agent_cpu = sum(process.cpu_percent for process in snapshot.processes)
        process_count = sum(process.process_count for process in snapshot.processes)
        self.sample_count += 1
        self.cpu_sum += snapshot.cpu_percent
        self.cpu_peak = max(self.cpu_peak, snapshot.cpu_percent)
        self.memory_sum += snapshot.memory_percent
        self.memory_peak = max(self.memory_peak, snapshot.memory_percent)
        self.swap_sum += snapshot.swap_percent
        self.swap_peak = max(self.swap_peak, snapshot.swap_percent)
        self.disk_sum += snapshot.disk_percent
        self.disk_peak = max(self.disk_peak, snapshot.disk_percent)
        self.agent_rss_sum += agent_rss
        self.agent_rss_peak = max(self.agent_rss_peak, agent_rss)
        self.agent_cpu_sum += agent_cpu
        self.agent_cpu_peak = max(self.agent_cpu_peak, agent_cpu)
        self.agent_process_count_peak = max(
            self.agent_process_count_peak, process_count
        )


class SystemMinuteWriter:
    """Fold one-second snapshots into restart-safe UTC minute rows."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._current: _MinuteAccumulator | None = None

    async def observe(self, snapshot: SystemSnapshot) -> None:
        minute = snapshot.ts - snapshot.ts % MINUTE_MS
        if self._current is None:
            self._current = _MinuteAccumulator.from_snapshot(snapshot)
            return
        if minute == self._current.minute_ms:
            self._current.add(snapshot)
            return
        if minute < self._current.minute_ms:
            # Wall clocks can move backwards. Mixing an old sample into the
            # current bucket would mislabel it; dropping one observation is
            # safer than reopening and writing an older bucket every second.
            log.warning(
                "metrics.out_of_order_sample",
                sample_ms=snapshot.ts,
                current_minute_ms=self._current.minute_ms,
            )
            return
        await self._persist(self._current)
        self._current = _MinuteAccumulator.from_snapshot(snapshot)

    async def close(self) -> None:
        current = self._current
        if current is None:
            return
        await self._persist(current)
        self._current = None

    async def _persist(self, bucket: _MinuteAccumulator) -> None:
        count = bucket.sample_count
        async with self._database.session() as db_session:
            row = await db_session.get(SystemMetricMinute, bucket.minute_ms)
            if row is None:
                row = SystemMetricMinute(
                    minute_ms=bucket.minute_ms,
                    sample_count=count,
                    cpu_avg_percent=bucket.cpu_sum / count,
                    cpu_peak_percent=bucket.cpu_peak,
                    memory_avg_percent=bucket.memory_sum / count,
                    memory_peak_percent=bucket.memory_peak,
                    swap_avg_percent=bucket.swap_sum / count,
                    swap_peak_percent=bucket.swap_peak,
                    disk_avg_percent=bucket.disk_sum / count,
                    disk_peak_percent=bucket.disk_peak,
                    agent_rss_avg_bytes=bucket.agent_rss_sum / count,
                    agent_rss_peak_bytes=bucket.agent_rss_peak,
                    agent_cpu_avg_percent=bucket.agent_cpu_sum / count,
                    agent_cpu_peak_percent=bucket.agent_cpu_peak,
                    agent_process_count_peak=bucket.agent_process_count_peak,
                )
            else:
                total = row.sample_count + count
                row.cpu_avg_percent = (
                    row.cpu_avg_percent * row.sample_count + bucket.cpu_sum
                ) / total
                row.cpu_peak_percent = max(row.cpu_peak_percent, bucket.cpu_peak)
                row.memory_avg_percent = (
                    row.memory_avg_percent * row.sample_count + bucket.memory_sum
                ) / total
                row.memory_peak_percent = max(
                    row.memory_peak_percent, bucket.memory_peak
                )
                row.swap_avg_percent = (
                    row.swap_avg_percent * row.sample_count + bucket.swap_sum
                ) / total
                row.swap_peak_percent = max(row.swap_peak_percent, bucket.swap_peak)
                row.disk_avg_percent = (
                    row.disk_avg_percent * row.sample_count + bucket.disk_sum
                ) / total
                row.disk_peak_percent = max(row.disk_peak_percent, bucket.disk_peak)
                row.agent_rss_avg_bytes = (
                    row.agent_rss_avg_bytes * row.sample_count + bucket.agent_rss_sum
                ) / total
                row.agent_rss_peak_bytes = max(
                    row.agent_rss_peak_bytes, bucket.agent_rss_peak
                )
                row.agent_cpu_avg_percent = (
                    row.agent_cpu_avg_percent * row.sample_count + bucket.agent_cpu_sum
                ) / total
                row.agent_cpu_peak_percent = max(
                    row.agent_cpu_peak_percent, bucket.agent_cpu_peak
                )
                row.agent_process_count_peak = max(
                    row.agent_process_count_peak, bucket.agent_process_count_peak
                )
                row.sample_count = total
            db_session.add(row)
            await db_session.commit()


class SystemProbe:
    """The synchronous psutil boundary, injectable for deterministic tests."""

    def __init__(self, api: Any = psutil) -> None:
        self._api = api

    def sample(
        self,
        *,
        ts: int,
        disk_path: Path,
        candidates: Sequence[ProcessCandidate],
    ) -> SystemSnapshot:
        cpu = float(self._api.cpu_percent(interval=None))
        per_core = tuple(
            float(value) for value in self._api.cpu_percent(interval=None, percpu=True)
        )
        memory = self._api.virtual_memory()
        swap = self._api.swap_memory()
        disk = self._api.disk_usage(str(disk_path))
        processes = tuple(
            metric
            for candidate in candidates
            if (metric := self._process_metric(candidate, ts)) is not None
        )
        return SystemSnapshot(
            ts=ts,
            cpu_percent=cpu,
            cpu_per_core=per_core,
            memory_total_bytes=int(memory.total),
            memory_used_bytes=int(memory.used),
            memory_available_bytes=int(memory.available),
            memory_percent=float(memory.percent),
            swap_total_bytes=int(swap.total),
            swap_used_bytes=int(swap.used),
            swap_free_bytes=int(swap.free),
            swap_percent=float(swap.percent),
            disk_total_bytes=int(disk.total),
            disk_used_bytes=int(disk.used),
            disk_free_bytes=int(disk.free),
            disk_percent=float(disk.percent),
            processes=processes,
        )

    def _process_metric(
        self, candidate: ProcessCandidate, ts: int
    ) -> AgentProcessMetric | None:
        try:
            root = self._api.Process(candidate.pid)
            members = [root, *root.children(recursive=True)]
            rss = 0
            cpu = 0.0
            counted = 0
            created_ms = ts
            for index, process in enumerate(members):
                try:
                    with process.oneshot():
                        if index == 0:
                            created_ms = int(process.create_time() * 1_000)
                        rss += int(process.memory_info().rss)
                        cpu += float(process.cpu_percent(interval=None))
                        counted += 1
                except (self._api.NoSuchProcess, self._api.AccessDenied):
                    # Processes regularly exit between children() and the
                    # detail reads. A disappearing child is a smaller tree,
                    # not a failed system snapshot.
                    continue
        except (self._api.NoSuchProcess, self._api.AccessDenied):
            return None
        return AgentProcessMetric(
            node_id=candidate.node_id,
            pid=candidate.pid,
            harness=candidate.harness,
            rss_bytes=rss,
            cpu_percent=cpu,
            uptime_ms=max(0, ts - created_ms),
            process_count=counted,
        )


class SystemSampler:
    """Sample psutil at a fixed cadence into a bounded in-memory ring."""

    def __init__(
        self,
        *,
        database: Database,
        disk_path: Path,
        interval_s: float = 1.0,
        capacity: int = 300,
        probe: SystemProbe | None = None,
        publish: Callable[[SystemSnapshot], Awaitable[None]] = _discard_snapshot,
    ) -> None:
        if interval_s <= 0:
            raise ValueError("system sample interval must be positive")
        if capacity <= 0:
            raise ValueError("system sample capacity must be positive")
        self._database = database
        self._disk_path = disk_path
        self._interval_s = interval_s
        self._probe = probe or SystemProbe()
        self._publish = publish
        self._history: deque[SystemSnapshot] = deque(maxlen=capacity)
        self._task: asyncio.Task[None] | None = None

    @property
    def history(self) -> tuple[SystemSnapshot, ...]:
        return tuple(self._history)

    @property
    def latest(self) -> SystemSnapshot | None:
        return self._history[-1] if self._history else None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> bool:
        if self.running:
            return False
        self._task = asyncio.create_task(self._run(), name="system-metrics")
        return True

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def sample_once(self) -> SystemSnapshot:
        candidates = await self._running_processes()
        snapshot = await asyncio.to_thread(
            self._probe.sample,
            ts=now_ms(),
            disk_path=self._disk_path,
            candidates=candidates,
        )
        self._history.append(snapshot)
        await self._publish(snapshot)
        return snapshot

    async def _running_processes(self) -> tuple[ProcessCandidate, ...]:
        async with self._database.session() as db_session:
            rows = (
                await db_session.exec(
                    select(col(Run.node_id), col(Run.pid), col(Run.harness))
                    .where(col(Run.status) == RunState.RUNNING)
                    .where(col(Run.pid).is_not(None))
                    .order_by(col(Run.node_id))
                )
            ).all()
        candidates: list[ProcessCandidate] = []
        for row in rows:
            pid = row[1]
            if pid is None:  # SQL predicate above; retained for the type boundary.
                continue
            candidates.append(
                ProcessCandidate(node_id=str(row[0]), pid=int(pid), harness=str(row[2]))
            )
        return tuple(candidates)

    async def _run(self) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.sample_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The sampler is observation, never authority. A transient
                # platform read must not kill the five-minute live ring.
                log.exception("metrics.sample_failed")
            remaining = self._interval_s - (asyncio.get_running_loop().time() - started)
            await asyncio.sleep(max(0, remaining))


__all__ = [
    "AgentProcessMetric",
    "ProcessCandidate",
    "SystemMinuteWriter",
    "SystemProbe",
    "SystemSampler",
    "SystemSnapshot",
]
