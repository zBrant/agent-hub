"""The AgentEvent contract itself, independent of any harness."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.harnesses.events import (
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
    agent_event_adapter,
)

RUN = "run_01JZZTEST"


def _one_of_each() -> list[object]:
    return [
        RunStarted(
            run_id=RUN,
            ts=1,
            harness="claude-code",
            model="claude-haiku-4-5",
            cwd=Path("/tmp/repo"),
            pid=42,
            session_id="sess-abc",
            harness_version="2.1.222",
        ),
        TurnStarted(run_id=RUN, ts=2, turn=1, model="claude-haiku-4-5"),
        AssistantText(run_id=RUN, ts=3, text="hello"),
        ToolCall(run_id=RUN, ts=4, call_id="toolu_1", tool="Write", input={"a": 1}),
        ToolResult(run_id=RUN, ts=5, call_id="toolu_1", ok=True, preview="done"),
        Usage(
            run_id=RUN,
            ts=6,
            model="claude-haiku-4-5-20251001",
            input_tokens=21,
            output_tokens=254,
            cache_read_tokens=21737,
            cache_write_tokens=6513,
            cache_write_1h_tokens=6513,
        ),
        TurnFinished(run_id=RUN, ts=7, turn=1, status="success"),
        RunFinished(run_id=RUN, ts=8, status="success", exit_code=0),
        RawChunk(run_id=RUN, ts=9, data=b"\x1b[2J\xff\xfe"),
    ]


def test_every_variant_round_trips_through_json() -> None:
    """Invariant 4: an NDJSON line must rebuild the exact event."""
    for event in _one_of_each():
        line = event.model_dump_json()  # type: ignore[attr-defined]
        restored = agent_event_adapter.validate_json(line)
        assert restored == event
        # And it really is one JSON object per line.
        assert "\n" not in line
        assert json.loads(line)["type"] == event.type  # type: ignore[attr-defined]


def test_union_discriminates_on_type() -> None:
    event = agent_event_adapter.validate_python(
        {"type": "assistant_text", "run_id": RUN, "ts": 1, "text": "hi"}
    )
    assert isinstance(event, AssistantText)


def test_unknown_field_is_rejected_rather_than_dropped() -> None:
    with pytest.raises(ValidationError):
        agent_event_adapter.validate_python(
            {"type": "usage", "run_id": RUN, "ts": 1, "model": "m", "input": 5}
        )


def test_events_are_frozen() -> None:
    event = AssistantText(run_id=RUN, ts=1, text="hi")
    with pytest.raises(ValidationError):
        event.text = "changed"  # type: ignore[misc]


def test_usage_counts_all_four_fields() -> None:
    usage = Usage(
        run_id=RUN,
        ts=1,
        model="m",
        input_tokens=21,
        output_tokens=254,
        cache_read_tokens=21737,
        cache_write_tokens=6513,
    )
    assert usage.total_tokens == 21 + 254 + 21737 + 6513


def test_usage_tier_split_must_sum_to_cache_write_tokens() -> None:
    with pytest.raises(ValidationError, match="tier split does not reconcile"):
        Usage(
            run_id=RUN,
            ts=1,
            model="m",
            cache_write_tokens=100,
            cache_write_5m_tokens=10,
            cache_write_1h_tokens=10,
        )


def test_usage_tier_split_is_optional() -> None:
    """A harness that reports no TTL breakdown must still validate."""
    usage = Usage(run_id=RUN, ts=1, model="m", cache_write_tokens=100)
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 0


def test_usage_tier_split_accepts_a_full_breakdown() -> None:
    usage = Usage(
        run_id=RUN,
        ts=1,
        model="m",
        cache_write_tokens=100,
        cache_write_5m_tokens=40,
        cache_write_1h_tokens=60,
    )
    assert usage.cache_write_5m_tokens + usage.cache_write_1h_tokens == (
        usage.cache_write_tokens
    )


def test_turn_finished_is_not_blocked_by_default() -> None:
    assert not TurnFinished(
        run_id=RUN, ts=1, turn=1, status="success"
    ).blocked_by_permission


def test_a_successful_turn_can_still_be_blocked_by_permission() -> None:
    """The whole point: Claude Code reports a refused run as a success."""
    turn = TurnFinished(
        run_id=RUN,
        ts=1,
        turn=1,
        status="success",
        permission_denials=(
            PermissionDenial(tool="Write", call_id="toolu_1", input={"file_path": "b"}),
        ),
    )
    assert turn.status == "success"
    assert turn.blocked_by_permission


def test_blocked_by_permission_survives_a_round_trip() -> None:
    turn = TurnFinished(
        run_id=RUN,
        ts=1,
        turn=1,
        status="success",
        permission_denials=(PermissionDenial(tool="Write"),),
    )
    restored = agent_event_adapter.validate_json(turn.model_dump_json())
    assert isinstance(restored, TurnFinished)
    assert restored.blocked_by_permission


def test_turn_numbers_are_one_based() -> None:
    with pytest.raises(ValidationError):
        TurnStarted(run_id=RUN, ts=1, turn=0, model="m")
