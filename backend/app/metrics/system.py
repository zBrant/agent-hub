"""Bounded local system telemetry sampled off the event loop."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import structlog
from sqlmodel import col, select

from app.models.clock import now_ms
from app.models.status import RunState
from app.models.tables import Run
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
    ) -> None:
        if interval_s <= 0:
            raise ValueError("system sample interval must be positive")
        if capacity <= 0:
            raise ValueError("system sample capacity must be positive")
        self._database = database
        self._disk_path = disk_path
        self._interval_s = interval_s
        self._probe = probe or SystemProbe()
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
    "SystemProbe",
    "SystemSampler",
    "SystemSnapshot",
]
