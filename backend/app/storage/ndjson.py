"""The run event log (invariant 4).

``runs/<run_id>/events.ndjson`` is the **source of truth** for a run. SQLite,
the dashboard, and every projection are derived from it and must always be
rebuildable from it. That constraint is what this module protects: if a value
exists only in a projection, the projection has taken on responsibility it is
not allowed to have.

The practical test is round-tripping. :func:`read_events` must return exactly
what :meth:`EventLog.append` was given — same variants, same field values, same
order. ``agenthub replay <run_id>`` (Phase 1) is that test run against real
history; :func:`verify_roundtrip` is the Phase 0 version of it.

All file I/O goes through ``asyncio.to_thread``. ``open``/``write``/``flush``
are blocking syscalls, and a stalled event loop stalls the PTY stream
(invariant 5).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import IO, Self

from pydantic import TypeAdapter, ValidationError

from app.harnesses.events import AgentEvent
from app.models.ids import RunId

EVENTS_FILENAME = "events.ndjson"

_ADAPTER: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)


class EventLogError(Exception):
    """The log on disk is unreadable or does not round-trip."""


def run_dir(runs_root: Path, run_id: RunId) -> Path:
    return runs_root / run_id


def events_path(runs_root: Path, run_id: RunId) -> Path:
    return run_dir(runs_root, run_id) / EVENTS_FILENAME


class EventLog:
    """Append-only writer for one run's event stream.

    Every append is flushed. A run that crashes must leave behind everything
    that happened up to the crash — a buffered tail lost on SIGKILL is exactly
    the history you need when diagnosing why the process died.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: IO[str] | None = None
        self._count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        return self._count

    async def open(self) -> Self:
        await asyncio.to_thread(self._path.parent.mkdir, parents=True, exist_ok=True)
        self._handle = await asyncio.to_thread(
            self._path.open, "a", encoding="utf-8", newline="\n"
        )
        return self

    async def append(self, event: AgentEvent) -> None:
        if self._handle is None:
            raise EventLogError("event log is not open")
        line = _ADAPTER.dump_json(event).decode("utf-8")
        if "\n" in line:
            # Would split one event across two records and corrupt every
            # subsequent read. Pydantic's JSON output is single-line, so this
            # is a guard against a future serializer change, not a live risk.
            raise EventLogError("serialized event contains a newline")
        await asyncio.to_thread(self._write_line, line)
        self._count += 1

    def _write_line(self, line: str) -> None:
        assert self._handle is not None
        self._handle.write(line)
        self._handle.write("\n")
        self._handle.flush()

    async def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            await asyncio.to_thread(handle.close)

    async def __aenter__(self) -> Self:
        return await self.open()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()


def read_events(path: Path) -> Iterator[AgentEvent]:
    """Replay a log from disk.

    Synchronous on purpose: replay is an offline operation (a CLI command, a
    test, a rebuild), never something on the request path. Callers inside the
    event loop must wrap it in ``asyncio.to_thread``.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise EventLogError(f"cannot read event log at {path}: {exc}") from exc

    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield _ADAPTER.validate_python(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise EventLogError(f"{path}:{lineno} does not parse: {exc}") from exc


def verify_roundtrip(path: Path, written: Sequence[AgentEvent]) -> None:
    """Assert the log on disk reproduces ``written`` exactly.

    Invariant 4 is only true if this passes. Phase 0 calls it at the end of
    every run: a write path that cannot be replayed is already broken, and
    finding that out now is cheaper than finding it out from a month-old run.
    """
    replayed = list(read_events(path))
    if len(replayed) != len(written):
        raise EventLogError(
            f"replay produced {len(replayed)} events, {len(written)} were written"
        )
    for index, (before, after) in enumerate(zip(written, replayed, strict=True)):
        if before != after:
            raise EventLogError(
                f"event {index} ({before.type}) did not survive the round trip:\n"
                f"  written : {before!r}\n"
                f"  replayed: {after!r}"
            )


def write_events_sync(path: Path, events: Iterable[AgentEvent]) -> int:
    """Write a whole log at once. Tests and fixtures only — never a live run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(_ADAPTER.dump_json(event).decode("utf-8"))
            handle.write("\n")
            count += 1
    return count
