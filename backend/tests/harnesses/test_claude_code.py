"""Golden tests for the Claude Code adapter.

The golden files are the point: they are what tells us that the next Claude Code
release changed the stream format, before a user does
(`docs/architecture.md` §10). Every ``.ndjson`` in
``tests/fixtures/claude-code/`` is real output from CLI 2.1.222 captured in
activity A3 — nothing here was written by hand.

Regenerate after a *deliberate* change, then read the diff::

    AGENTHUB_REGEN_GOLDEN=1 uv run pytest tests/harnesses/test_claude_code.py

A golden file blessed without reading it tests nothing.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Any

import pytest

from app.harnesses.base import ParseStats, RunHandle, RunSpec
from app.harnesses.claude_code import (
    CLI_COMMAND,
    IGNORED_LINES,
    STREAM_LIMIT,
    SUPPORTED_MODELS,
    TESTED_CLI_VERSION,
    TESTED_CLI_VERSIONS,
    ClaudeCodeAdapter,
    TokenTotals,
    build_argv,
    cumulative_conversation_usage,
    parse_stream,
)
from app.harnesses.events import (
    AgentEvent,
    AssistantText,
    RunFinished,
    RunStarted,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnFinished,
    TurnStarted,
    Usage,
    agent_event_adapter,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "claude-code"
FIXTURE_NAMES = sorted(path.stem for path in FIXTURES.glob("*.ndjson"))

RUN_ID = "run_01JGOLDEN0000000000000000"

# `system/init` and `result` lines carry no timestamp of their own, so those
# events fall back to our clock. Pin it so the goldens are byte-stable; the
# events that *do* have a harness timestamp keep their real one, which is why
# the two orders of magnitude differ in the expected files.
FROZEN_TS = 1_800_000_000_000


def _clock() -> int:
    return FROZEN_TS


def _load(name: str) -> tuple[list[AgentEvent], ParseStats]:
    stats = ParseStats()
    text = (FIXTURES / f"{name}.ndjson").read_text()
    events = list(parse_stream(text, run_id=RUN_ID, clock=_clock, stats=stats))
    return events, stats


def _snapshot(events: list[AgentEvent], stats: ParseStats) -> dict[str, Any]:
    return {
        "stats": {
            "lines": stats.lines,
            "events": stats.events,
            "ignored": dict(sorted(stats.ignored.items())),
            "unknown": dict(sorted(stats.unknown.items())),
            "malformed": stats.malformed,
            "zero_usage_turns": stats.zero_usage_turns,
            "usage_unreconciled_turns": stats.usage_unreconciled_turns,
        },
        "events": [event.model_dump(mode="json") for event in events],
    }


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_golden(name: str) -> None:
    events, stats = _load(name)
    actual = _snapshot(events, stats)
    expected_path = FIXTURES / f"{name}.expected.json"

    if os.environ.get("AGENTHUB_REGEN_GOLDEN"):
        expected_path.write_text(
            json.dumps(actual, indent=2, ensure_ascii=False) + "\n"
        )
        pytest.skip(f"regenerated {expected_path.name}")

    assert expected_path.exists(), (
        f"missing golden file {expected_path}; regenerate with AGENTHUB_REGEN_GOLDEN=1"
    )
    assert actual == json.loads(expected_path.read_text())


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_line_is_translated_or_ignored_on_purpose(name: str) -> None:
    """No fixture line may fall through unrecognized (invariant: no silent drop)."""
    _, stats = _load(name)
    assert stats.unknown == {}
    assert stats.malformed == 0
    assert stats.unhandled == 0
    for key in stats.ignored:
        assert key in IGNORED_LINES, f"{key} is dropped without a documented reason"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_events_round_trip_as_ndjson(name: str) -> None:
    """Invariant 4: SQLite must be rebuildable from events.ndjson."""
    events, _ = _load(name)
    for event in events:
        assert agent_event_adapter.validate_json(event.model_dump_json()) == event


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_parse_stream_never_invents_a_run_finished(name: str) -> None:
    """`exit_code` has no source in the JSON; only the adapter may synthesize it."""
    events, _ = _load(name)
    assert not [event for event in events if isinstance(event, RunFinished)]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_cache_write_tiers_reconcile(name: str) -> None:
    usages = [event for event in _load(name)[0] if isinstance(event, Usage)]
    assert usages
    for usage in usages:
        if usage.source == "reconstructed":
            # modelUsage publishes no TTL breakdown; pricing falls back to 1h.
            assert usage.cache_write_5m_tokens == 0
            assert usage.cache_write_1h_tokens == 0
            continue
        split = usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
        assert split == usage.cache_write_tokens
        # Every A3 capture is 100% on the expensive 1h tier — the note that
        # justifies keeping the split at all (design.md §4).
        assert usage.cache_write_5m_tokens == 0


def test_simple_edit_usage_defeats_both_token_traps() -> None:
    """5 assistant lines, 3 message ids, and an output count 50x too low.

    Summing `input_tokens` over lines gives 37 and over distinct message ids 21;
    summing `output_tokens` over either gives 5 against a real 254. Taking
    `result.usage` gives both correctly, once per turn.
    """
    events, _ = _load("simple_edit")
    usages = [event for event in events if isinstance(event, Usage)]
    assert len(usages) == 1
    usage = usages[0]
    assert usage.input_tokens == 21
    assert usage.output_tokens == 254
    assert usage.cache_read_tokens == 21737
    assert usage.cache_write_tokens == 6513
    # The dated spelling, not init's alias: two ids for one model in one file.
    assert usage.model == "claude-haiku-4-5-20251001"
    assert usage.source == "reported"


def test_no_usage_is_derived_from_assistant_lines() -> None:
    """One Usage per `result` line, never one per assistant message."""
    for name in FIXTURE_NAMES:
        events, _ = _load(name)
        usages = len([e for e in events if isinstance(e, Usage)])
        turns = len([e for e in events if isinstance(e, TurnFinished)])
        assert usages == turns, name


def test_multi_turn_is_one_run_with_two_turns() -> None:
    """`system/init` and `result` both repeat per turn with the same session_id."""
    events, _ = _load("multi_turn")
    started = [e for e in events if isinstance(e, RunStarted)]
    turn_started = [e for e in events if isinstance(e, TurnStarted)]
    turn_finished = [e for e in events if isinstance(e, TurnFinished)]
    assert len(started) == 1
    assert len(turn_started) == 2
    assert len(turn_finished) == 2
    assert [e.turn for e in turn_started] == [1, 2]
    assert [e.turn for e in turn_finished] == [1, 2]
    session_ids = {e.session_id for e in turn_started}
    assert session_ids == {started[0].session_id}


def test_permission_denied_looks_successful_and_is_not() -> None:
    events, _ = _load("permission_denied")
    (turn,) = [e for e in events if isinstance(e, TurnFinished)]
    # This is exactly what the CLI reports: success, is_error false, exit 0.
    assert turn.status == "success"
    assert turn.blocked_by_permission
    denial = turn.permission_denials[0]
    assert denial.tool == "Write"
    assert denial.call_id == "toolu_01QMPgrSLhrY9eemkUdGSzYy"
    assert denial.input["file_path"] == "/tmp/repo/b.txt"

    # And the refusal is distinguishable from a real tool failure at the call.
    denied = [e for e in events if isinstance(e, ToolResult) and e.denied]
    assert [e.call_id for e in denied] == [denial.call_id]
    assert not denied[0].ok


def test_a_real_tool_failure_is_not_a_denial() -> None:
    events, _ = _load("tool_error")
    failures = [e for e in events if isinstance(e, ToolResult) and not e.ok]
    assert len(failures) == 2
    assert not any(e.denied for e in failures)
    assert "File does not exist" in failures[0].preview
    (turn,) = [e for e in events if isinstance(e, TurnFinished)]
    assert not turn.blocked_by_permission


def test_budget_exhaustion_is_an_event_not_an_exception() -> None:
    events, _ = _load("budget_error")
    (turn,) = [e for e in events if isinstance(e, TurnFinished)]
    assert turn.status == "budget_exceeded"
    assert turn.summary is None
    assert turn.errors == ("Reached maximum budget ($0.001)",)


def test_budget_exhaustion_zeroes_result_usage_and_is_reconstructed() -> None:
    """`result.usage` is all zeros here while modelUsage reports real tokens.

    The only fixture where the primary source fails. The delta rule recovers
    the exact figures, the event is marked as reconstructed, and the recovery
    is counted so it cannot pass for a measurement.
    """
    events, stats = _load("budget_error")
    (usage,) = [e for e in events if isinstance(e, Usage)]
    assert usage.source == "reconstructed"
    assert usage.input_tokens == 10
    assert usage.output_tokens == 202
    assert usage.cache_read_tokens == 5472
    assert usage.cache_write_tokens == 3975
    # modelUsage has no TTL breakdown, so the tier split is unavailable.
    assert usage.cache_write_5m_tokens == 0
    assert usage.cache_write_1h_tokens == 0
    assert stats.zero_usage_turns == 1
    assert stats.usage_unreconciled_turns == 0

    # Every other fixture reports its own usage and needs no recovery.
    for name in FIXTURE_NAMES:
        if name == "budget_error":
            continue
        other_events, other_stats = _load(name)
        assert other_stats.zero_usage_turns == 0, name
        assert other_stats.usage_unreconciled_turns == 0, name
        assert all(
            e.source == "reported" for e in other_events if isinstance(e, Usage)
        ), name


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_model_usage_delta_reproduces_result_usage(name: str) -> None:
    """The rule behind the recovery, checked against the primary source.

    Undated `modelUsage` entries, cumulative, differenced turn over turn, equal
    `result.usage` on all four fields — exactly, on every turn that reports one.
    This is the test that fails if a future release changes `modelUsage`
    semantics, at which point the recovery in `_on_result` is no longer sound.
    """
    previous = TokenTotals()
    checked = 0
    for line in (FIXTURES / f"{name}.ndjson").read_text().splitlines():
        payload = json.loads(line)
        if payload.get("type") != "result":
            continue
        current = cumulative_conversation_usage(payload)
        delta = current - previous
        previous = current
        assert not delta.has_negative, f"{name}: cumulative counter went backwards"

        reported = payload.get("usage") or {}
        expected = TokenTotals(
            input_tokens=reported.get("input_tokens", 0),
            output_tokens=reported.get("output_tokens", 0),
            cache_read_tokens=reported.get("cache_read_input_tokens", 0),
            cache_write_tokens=reported.get("cache_creation_input_tokens", 0),
        )
        if expected.total == 0:
            # budget_error: nothing to compare against; that is the whole point.
            assert name == "budget_error"
            assert delta.total > 0
            continue
        assert delta == expected, f"{name}: delta {delta} != result.usage {expected}"
        checked += 1
    assert checked or name == "budget_error"


def test_the_side_channel_model_is_excluded_from_the_delta() -> None:
    """Including the dated key would inflate every recovered turn by ~530 in."""
    payload = json.loads(
        next(
            line
            for line in (FIXTURES / "simple_edit.ndjson").read_text().splitlines()
            if '"type":"result"' in line
        )
    )
    assert set(payload["modelUsage"]) == {
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
    }
    totals = cumulative_conversation_usage(payload)
    assert totals == TokenTotals(21, 254, 21737, 6513)
    assert payload["modelUsage"]["claude-haiku-4-5-20251001"]["inputTokens"] == 532


def test_a_backwards_cumulative_counter_emits_no_usage_at_all() -> None:
    """A resumed session or reordered lines break the delta's premise.

    Emitting the raw zero would under-report and emitting the absolute
    cumulative figure would over-report by an entire session. `usage_event` is
    append-only (`docs/architecture.md` §4), so a wrong row cannot be corrected
    later — a missing one can be rebuilt from the NDJSON.
    """

    def result_line(cumulative: int) -> str:
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "usage": {"input_tokens": 0},
                "modelUsage": {
                    "claude-haiku-4-5": {
                        "inputTokens": cumulative,
                        "outputTokens": 0,
                        "cacheReadInputTokens": 0,
                        "cacheCreationInputTokens": 0,
                    }
                },
            }
        )

    stats = ParseStats()
    events = list(
        parse_stream(
            [result_line(500), result_line(100)],
            run_id=RUN_ID,
            clock=_clock,
            stats=stats,
        )
    )
    usages = [e for e in events if isinstance(e, Usage)]
    assert len(usages) == 1  # the first turn recovered, the second did not
    assert usages[0].source == "reconstructed"
    assert usages[0].input_tokens == 500
    assert stats.zero_usage_turns == 2
    assert stats.usage_unreconciled_turns == 1
    # And the turn itself is still reported: a missing Usage is not a lost turn.
    assert len([e for e in events if isinstance(e, TurnFinished)]) == 2


def test_a_turn_that_really_consumed_nothing_stays_reported() -> None:
    stats = ParseStats()
    events = list(
        parse_stream(
            ['{"type":"result","subtype":"success","usage":{},"modelUsage":{}}'],
            run_id=RUN_ID,
            clock=_clock,
            stats=stats,
        )
    )
    (usage,) = [e for e in events if isinstance(e, Usage)]
    assert usage.source == "reported"
    assert usage.total_tokens == 0
    assert stats.zero_usage_turns == 0


def test_interrupt_is_a_status_not_a_crash() -> None:
    events, _ = _load("interrupted")
    (turn,) = [e for e in events if isinstance(e, TurnFinished)]
    assert turn.status == "interrupted"
    assert turn.summary is None
    # The "[Request interrupted by user]" user/text line is dropped on purpose:
    # the status already carries it.
    assert not any(
        isinstance(e, AssistantText) and "interrupted by user" in e.text for e in events
    )


def test_partial_messages_do_not_duplicate_text() -> None:
    """With --include-partial-messages every block arrives twice; we take one."""
    events, stats = _load("partial_messages")
    assert stats.ignored["stream_event"] == 26
    texts = [e.text for e in events if isinstance(e, AssistantText)]
    assert texts == ['The file a.txt contains the single word "hello".']
    assert len([e for e in events if isinstance(e, ThinkingDelta)]) == 1


def test_tool_calls_and_results_correlate() -> None:
    events, _ = _load("simple_edit")
    calls = {e.call_id: e for e in events if isinstance(e, ToolCall)}
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [e.tool for e in calls.values()] == ["Read", "Write"]
    assert results
    for result in results:
        assert result.call_id in calls
    assert calls["toolu_01BdF3Ujiaxy9kh7EMRk42Z4"].input == {
        "file_path": "/tmp/repo/b.txt",
        "content": "world",
    }


def test_thinking_signature_is_dropped() -> None:
    events, _ = _load("simple_edit")
    (thinking,) = [e for e in events if isinstance(e, ThinkingDelta)]
    assert thinking.text.startswith("The user wants me to:")
    assert "signature" not in thinking.model_dump()


def test_garbage_lines_are_surfaced_not_swallowed() -> None:
    stats = ParseStats()
    lines = [
        '{"type":"system","subtype":"init","session_id":"s","model":"m","cwd":"/tmp"}',
        "not json at all",
        '{"type":"brand_new_line_type_from_the_future","session_id":"s"}',
        "[1, 2, 3]",
        '{"type":"assistant","message":{"model":"m","content":[{"type":"hologram"}]}}',
        "",
    ]
    events = list(parse_stream(lines, run_id=RUN_ID, clock=_clock, stats=stats))

    assert [e.type for e in events] == ["run_started", "turn_started"]
    assert stats.malformed == 2  # the bare text and the JSON array
    assert stats.unknown == {
        "brand_new_line_type_from_the_future": 1,
        "assistant/block/hologram": 1,
    }
    assert stats.unhandled == 4
    assert stats.lines == 5  # the blank line is not a line


def test_a_result_without_an_init_still_numbers_its_turn() -> None:
    """Truncated capture or resumed session: never emit turn=0."""
    events = list(
        parse_stream(
            ['{"type":"result","subtype":"success","usage":{},"result":"ok"}'],
            run_id=RUN_ID,
            clock=_clock,
        )
    )
    (turn,) = [e for e in events if isinstance(e, TurnFinished)]
    assert turn.turn == 1


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_was_captured_from_a_version_we_claim_to_support(name: str) -> None:
    """The A3 capture spans two CLI versions; NOTES.md records only one.

    ``claude_code_version`` on ``system/init`` is 2.1.222 for four fixtures and
    2.1.223 for three — the binary auto-updated mid-session. Pin it so a
    re-capture cannot quietly widen the range the goldens actually cover.
    """
    (started,) = [e for e in _load(name)[0] if isinstance(e, RunStarted)]
    assert started.harness_version in TESTED_CLI_VERSIONS


def test_ignored_line_types_all_carry_a_reason() -> None:
    for key, reason in IGNORED_LINES.items():
        assert reason.strip(), key
        assert len(reason) > 40, f"{key}: a reason, not a label"


def test_argv_keeps_the_prompt_out_of_ps() -> None:
    spec = RunSpec(
        run_id=RUN_ID,
        cwd=Path("/tmp/repo"),
        prompt="a secret-bearing prompt",
        model="claude-haiku-4-5",
        launcher=("ai-jail", "--clean"),
    )
    argv = build_argv(spec)
    assert argv[:2] == ["ai-jail", "--clean"]
    assert argv[2] == CLI_COMMAND
    assert "a secret-bearing prompt" not in argv
    # Without this the CLI refuses every write in -p mode, silently (A3).
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--verbose" in argv  # stream-json requires it
    assert argv[argv.index("--model") + 1] == "claude-haiku-4-5"


def test_supported_models_match_the_pricing_catalog() -> None:
    adapter = ClaudeCodeAdapter()
    assert adapter.name == "claude-code"
    assert adapter.supported_models == list(SUPPORTED_MODELS)
    assert "claude-haiku-4-5" in adapter.supported_models
    assert adapter.stats == ParseStats()


async def _replay_process(path: Path, exit_code: int = 0) -> RunHandle:
    """A stand-in harness: replays a fixture on stdout, then exits."""
    script = (
        "import sys;"
        f"sys.stdout.write(open({str(path)!r}).read());"
        "sys.stdout.flush();"
        f"sys.exit({exit_code})"
    )
    process = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        script,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=STREAM_LIMIT,
    )
    return RunHandle(
        run_id=RUN_ID, argv=("python3", "-c"), process=process, started_ms=0
    )


async def test_adapter_appends_run_finished_from_the_process_exit() -> None:
    handle = await _replay_process(FIXTURES / "simple_edit.ndjson")
    adapter = ClaudeCodeAdapter()
    events = [event async for event in adapter.events(handle)]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].exit_code == 0
    assert events[-1].status == "success"
    assert events[-1].summary is not None and events[-1].summary.startswith("Done!")
    assert [e.type for e in events[:-1]] == [e.type for e in _load("simple_edit")[0]]
    assert adapter.stats.lines == 14
    assert adapter.stats.events == len(events) - 1
    assert adapter.stats.unhandled == 0


async def test_a_nonzero_exit_after_a_successful_turn_is_a_failure() -> None:
    handle = await _replay_process(FIXTURES / "simple_edit.ndjson", exit_code=2)
    events = [event async for event in ClaudeCodeAdapter().events(handle)]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "failed"
    assert events[-1].exit_code == 2


async def test_the_turn_reason_beats_the_exit_code() -> None:
    """`interrupted` exits 1; "failed" would lose why."""
    handle = await _replay_process(FIXTURES / "interrupted.ndjson", exit_code=1)
    events = [event async for event in ClaudeCodeAdapter().events(handle)]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "interrupted"


async def test_kill_terminates_the_process_group_as_interrupted() -> None:
    process = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        "import time; time.sleep(30)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    handle = RunHandle(run_id=RUN_ID, argv=("python3",), process=process, started_ms=0)
    adapter = ClaudeCodeAdapter()

    async def consume() -> list[AgentEvent]:
        return [event async for event in adapter.events(handle)]

    consuming = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await adapter.kill(handle)
    events = await asyncio.wait_for(consuming, timeout=5)

    assert process.returncode is not None
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "interrupted"


async def test_cancelling_the_stream_kills_the_process_tree() -> None:
    process = await asyncio.create_subprocess_exec(
        "python3",
        "-c",
        "import time; time.sleep(30)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    handle = RunHandle(run_id=RUN_ID, argv=("python3",), process=process, started_ms=0)

    async def consume() -> None:
        async for _ in ClaudeCodeAdapter().events(handle):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.wait_for(process.wait(), timeout=5) != 0


async def test_send_and_interrupt_point_at_the_phase_1_recipe() -> None:
    adapter = ClaudeCodeAdapter()
    handle = await _replay_process(FIXTURES / "simple_edit.ndjson")
    try:
        with pytest.raises(NotImplementedError, match="input-format stream-json"):
            await adapter.send(handle, "hello")
        with pytest.raises(NotImplementedError, match="control_request"):
            await adapter.interrupt(handle)
    finally:
        await adapter.kill(handle)


@pytest.mark.harness
def test_installed_cli_version_has_not_drifted() -> None:
    """The fixtures are only evidence for the version they were captured from.

    A patch bump warns; a minor bump fails, because that is when the stream
    format has historically moved and the goldens need re-capturing (A3).
    """
    if shutil.which(CLI_COMMAND) is None:
        pytest.skip(f"{CLI_COMMAND} is not installed")
    output = subprocess.run(
        [CLI_COMMAND, "--version"], capture_output=True, text=True, timeout=60
    ).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    assert match, f"unparseable version output: {output!r}"
    installed = tuple(int(part) for part in match.groups())
    tested = tuple(int(part) for part in TESTED_CLI_VERSION.split("."))
    assert installed[:2] == tested[:2], (
        f"claude {'.'.join(map(str, installed))} vs fixtures captured from "
        f"{TESTED_CLI_VERSION}: re-run activity A3 before trusting the goldens"
    )
    if installed != tested:
        warnings.warn(
            f"claude {'.'.join(map(str, installed))} != tested {TESTED_CLI_VERSION}",
            stacklevel=1,
        )
