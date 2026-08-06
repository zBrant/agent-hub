"""Tests for the run event log.

The round-trip tests are invariant 4 stated executably: if a written log cannot
rebuild the exact event stream, every projection derived from it is suspect.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.harnesses.events import (
    AgentEvent,
    AssistantText,
    PermissionDenial,
    RawChunk,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
    TurnFinished,
    TurnStarted,
    Usage,
)
from app.storage.ndjson import (
    EVENTS_FILENAME,
    EventLog,
    EventLogError,
    events_path,
    read_events,
    verify_roundtrip,
    write_events_sync,
)

RUN_ID = "run_01J000000000000000000000"


def sample_events() -> list[AgentEvent]:
    """One of every variant that Phase 0 can produce, plus the awkward ones."""
    return [
        RunStarted(
            run_id=RUN_ID,
            ts=1,
            harness="claude-code",
            model="claude-haiku-4-5",
            cwd=Path("/tmp/wt"),
            pid=4242,
            session_id="sess-abc",
        ),
        TurnStarted(run_id=RUN_ID, ts=2, turn=1, model="claude-haiku-4-5"),
        AssistantText(run_id=RUN_ID, ts=3, text="hello\twith\ttabs and ünicode ✓"),
        ToolCall(
            run_id=RUN_ID,
            ts=4,
            call_id="toolu_1",
            tool="Write",
            input={"file_path": "/tmp/wt/a.txt", "content": "x" * 100},
        ),
        ToolResult(run_id=RUN_ID, ts=5, call_id="toolu_1", ok=True, preview="wrote"),
        Usage(
            run_id=RUN_ID,
            ts=6,
            model="claude-haiku-4-5-20251001",
            input_tokens=21,
            output_tokens=254,
            cache_read_tokens=21_737,
            cache_write_tokens=6_513,
            cache_write_1h_tokens=6_513,
        ),
        TurnFinished(
            run_id=RUN_ID,
            ts=7,
            turn=1,
            status="success",
            permission_denials=(
                PermissionDenial(tool="Write", call_id="toolu_2", input={"a": 1}),
            ),
            errors=("something went wrong",),
        ),
        RawChunk(run_id=RUN_ID, ts=8, data=b"\x1b[2J\xff\xfe not utf-8"),
        RunFinished(run_id=RUN_ID, ts=9, status="success", exit_code=0),
    ]


async def test_append_then_replay_is_lossless(tmp_path: Path) -> None:
    path = tmp_path / EVENTS_FILENAME
    events = sample_events()

    async with EventLog(path) as log:
        for event in events:
            await log.append(event)
        assert log.count == len(events)

    verify_roundtrip(path, events)
    assert list(read_events(path)) == events


async def test_raw_bytes_survive(tmp_path: Path) -> None:
    """PTY output is not valid UTF-8; the log must not mangle it."""
    path = tmp_path / EVENTS_FILENAME
    original = RawChunk(run_id=RUN_ID, ts=1, data=bytes(range(256)))
    async with EventLog(path) as log:
        await log.append(original)

    (replayed,) = list(read_events(path))
    assert isinstance(replayed, RawChunk)
    assert replayed.data == original.data


async def test_one_line_per_event(tmp_path: Path) -> None:
    """A multi-line record would desynchronize every subsequent read."""
    path = tmp_path / EVENTS_FILENAME
    events = sample_events()
    async with EventLog(path) as log:
        for event in events:
            await log.append(event)

    assert len(path.read_text(encoding="utf-8").splitlines()) == len(events)


async def test_each_append_is_durable(tmp_path: Path) -> None:
    """A killed run must leave behind everything up to the kill."""
    path = tmp_path / EVENTS_FILENAME
    log = await EventLog(path).open()
    await log.append(sample_events()[0])

    # Read from a different handle, without closing the writer.
    assert len(list(read_events(path))) == 1
    await log.close()


async def test_append_reopens_rather_than_truncates(tmp_path: Path) -> None:
    path = tmp_path / EVENTS_FILENAME
    first, second = sample_events()[:2]

    async with EventLog(path) as log:
        await log.append(first)
    async with EventLog(path) as log:
        await log.append(second)

    assert list(read_events(path)) == [first, second]


async def test_append_before_open_raises(tmp_path: Path) -> None:
    with pytest.raises(EventLogError, match="not open"):
        await EventLog(tmp_path / EVENTS_FILENAME).append(sample_events()[0])


async def test_creates_the_run_directory(tmp_path: Path) -> None:
    path = events_path(tmp_path / "runs", RUN_ID)
    assert not path.parent.exists()
    async with EventLog(path) as log:
        await log.append(sample_events()[0])
    assert path.exists()


def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / EVENTS_FILENAME
    written = write_events_sync(path, sample_events()[:2])
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    assert len(list(read_events(path))) == written


def test_corrupt_line_names_the_line(tmp_path: Path) -> None:
    path = tmp_path / EVENTS_FILENAME
    write_events_sync(path, sample_events()[:2])
    path.write_text(
        path.read_text(encoding="utf-8") + "{not json\n",
        encoding="utf-8",
    )
    with pytest.raises(EventLogError, match=r":3 does not parse"):
        list(read_events(path))


def test_unknown_event_type_fails_loudly(tmp_path: Path) -> None:
    """Replay must refuse a log it does not fully understand."""
    path = tmp_path / EVENTS_FILENAME
    path.write_text(
        '{"type":"from_the_future","run_id":"r","ts":1}\n', encoding="utf-8"
    )
    with pytest.raises(EventLogError, match="does not parse"):
        list(read_events(path))


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(EventLogError, match="cannot read"):
        list(read_events(tmp_path / "nope.ndjson"))


def test_roundtrip_verification_catches_a_short_log(tmp_path: Path) -> None:
    path = tmp_path / EVENTS_FILENAME
    events = sample_events()
    write_events_sync(path, events[:-1])
    with pytest.raises(EventLogError, match="replay produced"):
        verify_roundtrip(path, events)


def test_roundtrip_verification_catches_a_changed_field(tmp_path: Path) -> None:
    path = tmp_path / EVENTS_FILENAME
    events = sample_events()
    write_events_sync(path, events)
    tampered = list(events)
    tampered[2] = AssistantText(run_id=RUN_ID, ts=3, text="not what was written")
    with pytest.raises(EventLogError, match="did not survive"):
        verify_roundtrip(path, tampered)
