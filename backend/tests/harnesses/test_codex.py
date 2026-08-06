"""Golden and contract tests for the Codex CLI adapter."""

from __future__ import annotations

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

from app.harnesses import ADAPTERS, UnknownHarnessError, create_adapter
from app.harnesses.base import ParseStats, RunHandle, RunSpec
from app.harnesses.codex import (
    CLI_COMMAND,
    DEFAULT_MODEL,
    IGNORED_LINES,
    STREAM_LIMIT,
    SUPPORTED_MODELS,
    TESTED_CLI_VERSION,
    CodexAdapter,
    build_argv,
    parse_stream,
)
from app.harnesses.events import (
    AgentEvent,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
    TurnFinished,
    Usage,
    agent_event_adapter,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "codex"
FIXTURE_NAMES = sorted(path.stem for path in FIXTURES.glob("*.ndjson"))
RUN_ID = "run_01JCODEX00000000000000000"
FROZEN_TS = 1_800_000_000_000


def _clock() -> int:
    return FROZEN_TS


def _load(name: str) -> tuple[list[AgentEvent], ParseStats]:
    stats = ParseStats()
    events = list(
        parse_stream(
            (FIXTURES / f"{name}.ndjson").read_text(),
            run_id=RUN_ID,
            clock=_clock,
            stats=stats,
        )
    )
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
        f"missing {expected_path}; regenerate with AGENTHUB_REGEN_GOLDEN=1"
    )
    assert actual == json.loads(expected_path.read_text())


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_every_event_round_trips_through_the_union(name: str) -> None:
    for event in _load(name)[0]:
        assert agent_event_adapter.validate_json(event.model_dump_json()) == event


def test_simple_edit_maps_file_and_command_tools() -> None:
    events, stats = _load("simple_edit")
    assert stats.unhandled == 0
    calls = [event for event in events if isinstance(event, ToolCall)]
    results = [event for event in events if isinstance(event, ToolResult)]
    assert [call.tool for call in calls] == ["file_change", "shell"]
    assert [result.ok for result in results] == [True, True]
    assert calls[0].input == {"changes": [{"path": "/tmp/repo/b.txt", "kind": "add"}]}


def test_failed_command_is_a_tool_result_not_a_failed_turn() -> None:
    events, _ = _load("tool_error")
    (result,) = [event for event in events if isinstance(event, ToolResult)]
    (turn,) = [event for event in events if isinstance(event, TurnFinished)]
    assert not result.ok
    assert "No such file or directory" in result.preview
    assert turn.status == "success"  # the agent handled and reported the failure


def test_usage_breakdowns_are_not_double_counted() -> None:
    events, _ = _load("simple_edit")
    (usage,) = [event for event in events if isinstance(event, Usage)]
    assert usage.input_tokens == 27_777 - 19_968
    assert usage.cache_read_tokens == 19_968
    assert usage.cache_write_tokens == 0
    assert usage.output_tokens == 177  # already includes 20 reasoning tokens
    assert usage.total_tokens == 27_777 + 177


def test_resumed_session_reuses_thread_and_emits_per_turn_usage() -> None:
    events, stats = _load("multi_turn")
    starts = [event for event in events if isinstance(event, RunStarted)]
    usages = [event for event in events if isinstance(event, Usage)]
    turns = [event for event in events if isinstance(event, TurnFinished)]
    assert len(starts) == 1
    assert [turn.turn for turn in turns] == [1, 2]
    assert [usage.input_tokens for usage in usages] == [7_445, 10_985]
    assert stats.ignored == {"thread.started/repeated": 1}


def test_interrupted_capture_is_truthfully_partial() -> None:
    events, stats = _load("interrupted")
    assert stats.unhandled == 0
    assert not any(isinstance(event, Usage | TurnFinished) for event in events)
    (call,) = [event for event in events if isinstance(event, ToolCall)]
    assert call.input == {"command": "/bin/zsh -lc 'sleep 120'"}


def test_unreconciled_usage_is_omitted() -> None:
    line = json.dumps(
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 11,
                "cache_write_input_tokens": 0,
                "output_tokens": 1,
            },
        }
    )
    stats = ParseStats()
    events = list(parse_stream([line], run_id=RUN_ID, stats=stats, clock=_clock))
    assert not any(isinstance(event, Usage) for event in events)
    assert any(isinstance(event, TurnFinished) for event in events)
    assert stats.usage_unreconciled_turns == 1


def test_unknown_and_malformed_lines_are_loud() -> None:
    stats = ParseStats()
    lines = [
        "not json",
        "[]",
        '{"type":"future.event"}',
        '{"type":"item.completed","item":{"id":"1","type":"hologram"}}',
    ]
    assert not list(parse_stream(lines, run_id=RUN_ID, stats=stats))
    assert stats.malformed == 2
    assert stats.unknown == {"future.event": 1, "item.completed/hologram": 1}


def test_ignored_shapes_have_real_reasons() -> None:
    assert all(len(reason) > 40 for reason in IGNORED_LINES.values())


def test_argv_is_deterministic_and_prompt_free() -> None:
    spec = RunSpec(
        run_id=RUN_ID,
        cwd=Path("/tmp/repo"),
        prompt="secret-bearing prompt",
        model=DEFAULT_MODEL,
        launcher=("ai-jail", "--exec"),
    )
    argv = build_argv(spec)
    assert argv[:3] == ["ai-jail", "--exec", CLI_COMMAND]
    assert argv[-1] == "-"
    assert "secret-bearing prompt" not in argv
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv


def test_unsandboxed_contract_invocation_uses_codex_workspace_sandbox() -> None:
    spec = RunSpec(run_id=RUN_ID, cwd=Path("/tmp/repo"), prompt="hi")
    argv = build_argv(spec)
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


def test_budget_and_unknown_models_fail_before_launch() -> None:
    with pytest.raises(ValueError, match="max-budget"):
        build_argv(
            RunSpec(
                run_id=RUN_ID,
                cwd=Path("/tmp/repo"),
                prompt="hi",
                max_budget_usd=1,
            )
        )
    with pytest.raises(ValueError, match="unsupported Codex model"):
        build_argv(
            RunSpec(
                run_id=RUN_ID,
                cwd=Path("/tmp/repo"),
                prompt="hi",
                model="future-model",
            )
        )


async def _replay_process(path: Path, exit_code: int = 0) -> RunHandle:
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
        run_id=RUN_ID,
        argv=("python3", "-c"),
        process=process,
        started_ms=0,
        model=DEFAULT_MODEL,
        cwd=Path("/tmp/repo"),
    )


async def test_adapter_synthesizes_process_exit() -> None:
    handle = await _replay_process(FIXTURES / "simple_edit.ndjson")
    adapter = CodexAdapter()
    events = [event async for event in adapter.events(handle)]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "success"
    assert events[-1].exit_code == 0
    assert adapter.stats.unhandled == 0


async def test_adapter_remembers_operator_interruption() -> None:
    handle = await _replay_process(FIXTURES / "interrupted.ndjson", exit_code=1)
    handle.interrupted = True
    events = [event async for event in CodexAdapter().events(handle)]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "interrupted"
    assert events[-1].exit_code == 1


@pytest.mark.harness
async def test_real_codex_roundtrip(tmp_path: Path) -> None:
    if not os.environ.get("AGENTHUB_RUN_LIVE_HARNESS"):
        pytest.skip("set AGENTHUB_RUN_LIVE_HARNESS=1 to spend a real Codex turn")
    if shutil.which(CLI_COMMAND) is None:
        pytest.skip("codex is not installed")
    git = await asyncio.create_subprocess_exec(
        "git", "init", "-q", "-b", "main", str(tmp_path)
    )
    assert await git.wait() == 0
    spec = RunSpec(
        run_id=RUN_ID,
        cwd=tmp_path,
        prompt="Create contract.txt containing exactly: contract works",
        model=DEFAULT_MODEL,
    )
    adapter = CodexAdapter()
    handle = await adapter.start(spec)
    events = [event async for event in adapter.events(handle)]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "success"
    assert (tmp_path / "contract.txt").read_text().strip() == "contract works"
    assert adapter.stats.unhandled == 0


@pytest.mark.harness
def test_installed_cli_version_has_not_drifted() -> None:
    if shutil.which(CLI_COMMAND) is None:
        pytest.skip("codex is not installed")
    output = subprocess.run(
        [CLI_COMMAND, "--version"], capture_output=True, text=True, timeout=60
    ).stdout
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
    assert match, f"unparseable version output: {output!r}"
    installed = tuple(int(part) for part in match.groups())
    tested = tuple(int(part) for part in TESTED_CLI_VERSION.split("."))
    assert installed[:2] == tested[:2], (
        f"codex {'.'.join(map(str, installed))} vs fixtures from "
        f"{TESTED_CLI_VERSION}: recapture before trusting goldens"
    )
    if installed != tested:
        warnings.warn(
            f"codex {'.'.join(map(str, installed))} != tested {TESTED_CLI_VERSION}",
            stacklevel=1,
        )


def test_adapter_catalog_is_current_family() -> None:
    adapter = CodexAdapter()
    assert adapter.name == "codex"
    assert adapter.supported_models == list(SUPPORTED_MODELS)
    assert DEFAULT_MODEL == "gpt-5.6-sol"


def test_registry_constructs_codex_without_leaking_dispatch_upward() -> None:
    assert set(ADAPTERS) == {"claude-code", "codex"}
    assert isinstance(create_adapter("codex"), CodexAdapter)
    with pytest.raises(UnknownHarnessError, match="available"):
        create_adapter("future")
