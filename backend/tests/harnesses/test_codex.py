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
from app.harnesses.base import (
    HarnessError,
    ParseStats,
    RunHandle,
    RunSpec,
    StructuredRequest,
    StructuredResult,
    supports_structured_output,
)
from app.harnesses.codex import (
    CLI_COMMAND,
    DEFAULT_MODEL,
    IGNORED_LINES,
    STREAM_LIMIT,
    SUPPORTED_MODELS,
    TESTED_CLI_VERSION,
    CodexAdapter,
    build_argv,
    build_structured_argv,
    parse_stream,
    structured_prompt,
    structured_result,
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
from tests.harnesses.fake_cli import fake_cli, read_probe

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
    handle = RunHandle(
        run_id=RUN_ID,
        argv=("python3",),
        process=process,
        started_ms=0,
        model=DEFAULT_MODEL,
        cwd=Path("/tmp/repo"),
    )
    adapter = CodexAdapter()

    async def consume() -> list[AgentEvent]:
        return [event async for event in adapter.events(handle)]

    consuming = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    await adapter.kill(handle)
    events = await asyncio.wait_for(consuming, timeout=5)

    assert process.returncode is not None
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "interrupted"


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


# ---------------------------------------------------------------------------
# Structured output (`--output-schema`). Verified on 0.146.0 — the help text
# says only "Path to a JSON Schema file"; the module docstring records the three
# things it does not say.
# ---------------------------------------------------------------------------

# A real capture, byte for byte apart from the line wrapping. Produced by:
#
#     printf '<prompt>' | codex exec --json --color never --ignore-user-config \
#       --ignore-rules --skip-git-repo-check --ephemeral --sandbox read-only \
#       --model gpt-5.6-sol --output-schema schema.json -
#
# Note what is *not* in it: no `structured_output`, no dedicated field of any
# kind. The schema's payload is the text of the final agent message.
CAPTURED_STRUCTURED_STREAM = "\n".join(
    [
        r'{"type":"thread.started","thread_id":"019fe36d-f1ff-7d22-9f4b-f5f0475"}',
        r'{"type":"turn.started"}',
        r'{"type":"item.completed","item":{"id":"item_0","type":"agent_message",'
        r'"text":"{\"color\":\"Red\",\"hex\":\"#FF0000\"}"}}',
        r'{"type":"turn.completed","usage":{"input_tokens":13667,'
        r'"cached_input_tokens":0,"cache_write_input_tokens":0,'
        r'"output_tokens":22,"reasoning_output_tokens":0}}',
    ]
)

# The provider's verbatim complaint when the same probe sent a schema that was
# not OpenAI-strict. Reproduced twice more for a `required` array that did not
# list every key. This is why the module docstring tells callers to emit strict
# schemas instead of having the adapter rewrite theirs.
CAPTURED_SCHEMA_REJECTION = json.dumps(
    {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "code": "invalid_json_schema",
            "message": (
                "Invalid schema for response_format 'codex_output_schema': In "
                "context=(), 'additionalProperties' is required to be supplied "
                "and to be false."
            ),
            "param": "text.format.schema",
        },
        "status": 400,
    },
    indent=2,
)

CAPTURED_REJECTED_STREAM = "\n".join(
    json.dumps(line)
    for line in (
        {"type": "thread.started", "thread_id": "019fe37a-6c57-74d1-8568-ef8e37a"},
        {"type": "turn.started"},
        {"type": "error", "message": CAPTURED_SCHEMA_REJECTION},
        {"type": "turn.failed", "error": {"message": CAPTURED_SCHEMA_REJECTION}},
    )
)

STRUCTURED_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"color": {"type": "string"}, "hex": {"type": "string"}},
    "required": ["color", "hex"],
    "additionalProperties": False,
}

STRUCTURED_PROMPT = "Name one primary color and its hex."


def _structured_request(**overrides: Any) -> StructuredRequest:
    fields: dict[str, Any] = {
        "prompt": STRUCTURED_PROMPT,
        "schema": STRUCTURED_SCHEMA,
    }
    fields.update(overrides)
    return StructuredRequest(**fields)


def _complete(
    stdout: str, exit_code: int = 0, model: str = DEFAULT_MODEL
) -> StructuredResult:
    return structured_result(
        stdout,
        exit_code,
        model=model,
        cwd=Path("/tmp/repo"),
        stats=ParseStats(),
    )


def _turn_completed(**usage: int) -> str:
    return json.dumps({"type": "turn.completed", "usage": usage})


def test_the_adapter_advertises_the_capability_rather_than_its_name() -> None:
    assert supports_structured_output(CodexAdapter())


def test_structured_argv_keeps_the_prompt_out_of_ps() -> None:
    request = _structured_request(
        system="secret-bearing system prompt",
        cwd=Path("/tmp/repo"),
        launcher=("ai-jail", "--clean"),
    )
    argv = build_structured_argv(request, schema_file=Path("/tmp/schema.json"))

    assert argv[:3] == ["ai-jail", "--clean", CLI_COMMAND]
    assert argv[3] == "exec"
    assert argv[-1] == "-"
    assert STRUCTURED_PROMPT not in argv
    assert "secret-bearing system prompt" not in argv
    assert argv[argv.index("--output-schema") + 1] == "/tmp/schema.json"
    assert argv[argv.index("--cd") + 1] == "/tmp/repo"
    # Externally sandboxed already; nesting sandboxes fails on macOS.
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv


def test_structured_argv_without_a_launcher_is_read_only() -> None:
    """Answering a schema writes nothing, so `workspace-write` would be too wide."""
    argv = build_structured_argv(
        _structured_request(), schema_file=Path("/tmp/schema.json")
    )
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    # A StructuredRequest has no worktree, so its cwd need not be a repository.
    assert "--skip-git-repo-check" in argv
    assert "--cd" not in argv


def test_structured_unknown_model_fails_before_launch() -> None:
    with pytest.raises(ValueError, match="unsupported Codex model"):
        build_structured_argv(
            _structured_request(model="future-model"),
            schema_file=Path("/tmp/schema.json"),
        )


def test_structured_system_text_is_folded_into_the_prompt() -> None:
    """`codex exec` has no system-prompt flag of any kind on 0.146.0."""
    assert structured_prompt(_structured_request()) == STRUCTURED_PROMPT
    folded = structured_prompt(_structured_request(system="Be terse."))
    assert "# Instructions\n\nBe terse." in folded
    assert f"# Task\n\n{STRUCTURED_PROMPT}" in folded


def test_structured_answer_is_the_final_agent_message() -> None:
    """There is no structured field; the help text does not say so."""
    result = _complete(CAPTURED_STRUCTURED_STREAM)
    assert result.data == {"color": "Red", "hex": "#FF0000"}
    assert result.model == DEFAULT_MODEL


def test_structured_answer_prefers_the_last_agent_message() -> None:
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "a", "type": "agent_message", "text": '{"n":1}'},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "b", "type": "agent_message", "text": '{"n":2}'},
                }
            ),
            _turn_completed(input_tokens=1, output_tokens=1),
        ]
    )
    assert _complete(stream).data == {"n": 2}


def test_structured_usage_carries_all_four_fields() -> None:
    usage = _complete(CAPTURED_STRUCTURED_STREAM).usage
    assert usage is not None
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_tokens,
        usage.cache_write_tokens,
    ) == (13_667, 22, 0, 0)
    assert usage.total_tokens == 13_689
    assert usage.model == DEFAULT_MODEL


def test_structured_usage_does_not_double_count_the_input_breakdown() -> None:
    """`input_tokens` is the total; cached and cache-write are carved out of it.

    Same rule as the run path, and the capture above cannot catch a regression
    because both cache fields are zero in it.
    """
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "a",
                        "type": "agent_message",
                        "text": '{"color":"Red","hex":"#FF0000"}',
                    },
                }
            ),
            _turn_completed(
                input_tokens=1_000,
                cached_input_tokens=600,
                cache_write_input_tokens=100,
                output_tokens=50,
            ),
        ]
    )
    usage = _complete(stream).usage
    assert usage is not None
    assert usage.input_tokens == 300
    assert usage.cache_read_tokens == 600
    assert usage.cache_write_tokens == 100
    assert usage.output_tokens == 50
    assert usage.total_tokens == 1_050


def test_structured_usage_sums_the_turns_it_was_given() -> None:
    stream = "\n".join(
        [
            _turn_completed(input_tokens=10, output_tokens=1),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"id": "a", "type": "agent_message", "text": "{}"},
                }
            ),
            _turn_completed(input_tokens=20, output_tokens=2),
        ]
    )
    usage = _complete(stream).usage
    assert usage is not None
    assert usage.input_tokens == 30
    assert usage.output_tokens == 3


def test_structured_usage_is_none_when_the_harness_reported_none() -> None:
    """Allowed by `StructuredResult`, and better than inventing a zero.

    A turn with no `turn.completed` published no usage at all; the caller reads
    `None` as "not accounted", not as "free".
    """
    stream = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "a", "type": "agent_message", "text": '{"n":1}'},
        }
    )
    assert _complete(stream).usage is None


def test_structured_nonzero_exit_quotes_the_provider_rejection() -> None:
    """A non-strict schema is a 400. Without the text it looks like a network fault."""
    with pytest.raises(HarnessError) as raised:
        _complete(CAPTURED_REJECTED_STREAM, exit_code=1)
    message = str(raised.value)
    assert "exited 1" in message
    assert "invalid_json_schema" in message
    # `error` and `turn.failed` carry the identical text; say it once.
    assert message.count("invalid_json_schema") == 1


def test_structured_missing_final_message_is_a_harness_error() -> None:
    with pytest.raises(HarnessError, match="no agent message"):
        _complete(_turn_completed(input_tokens=10, output_tokens=1))


def test_structured_unparseable_final_message_is_a_harness_error() -> None:
    def stream(text: str) -> str:
        return json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "a", "type": "agent_message", "text": text},
            }
        )

    with pytest.raises(HarnessError, match="unparseable final message"):
        _complete(stream("Sure! Here is the JSON you asked for."))
    with pytest.raises(HarnessError, match="expected a JSON object"):
        _complete(stream("[1, 2]"))


def test_structured_errors_never_quote_the_prompt_or_the_answer() -> None:
    """conventions §6: an error message is not a place for untrusted content."""
    secret = "swordfish-" + "x" * 8
    answer = json.dumps(
        {
            "type": "item.completed",
            "item": {"id": "a", "type": "agent_message", "text": secret},
        }
    )
    for stdout, exit_code in ((answer, 0), (CAPTURED_STRUCTURED_STREAM, 1)):
        with pytest.raises(HarnessError) as raised:
            _complete(stdout, exit_code=exit_code)
        assert secret not in str(raised.value)


async def test_structured_sends_the_prompt_on_stdin_and_cleans_up(
    tmp_path: Path,
) -> None:
    record = tmp_path / "probe.json"
    request = _structured_request(
        system="Be terse.",
        launcher=fake_cli(stdout=CAPTURED_STRUCTURED_STREAM, record=record),
    )
    adapter = CodexAdapter()
    result = await adapter.complete_structured(request)
    probe = read_probe(record)

    assert result.data == {"color": "Red", "hex": "#FF0000"}
    assert probe.argv[0] == CLI_COMMAND
    assert STRUCTURED_PROMPT in probe.stdin
    assert "Be terse." in probe.stdin
    assert STRUCTURED_PROMPT not in probe.argv
    assert json.loads(probe.file_content("--output-schema") or "") == STRUCTURED_SCHEMA
    # The schema file existed for exactly as long as the process did.
    assert not probe.path_of("--output-schema").exists()
    assert adapter.stats.unhandled == 0


async def test_structured_reports_a_dead_cli_rather_than_a_half_result(
    tmp_path: Path,
) -> None:
    request = _structured_request(
        launcher=fake_cli(stdout=CAPTURED_REJECTED_STREAM, exit_code=1),
    )
    with pytest.raises(HarnessError, match="invalid_json_schema"):
        await CodexAdapter().complete_structured(request)


@pytest.mark.harness
async def test_real_codex_answers_a_schema(tmp_path: Path) -> None:
    """One live turn on the default model. Paid, so it is gated twice."""
    if not os.environ.get("AGENTHUB_RUN_LIVE_HARNESS"):
        pytest.skip("set AGENTHUB_RUN_LIVE_HARNESS=1 to spend a real Codex turn")
    if shutil.which(CLI_COMMAND) is None:
        pytest.skip("codex is not installed")

    adapter = CodexAdapter()
    result = await adapter.complete_structured(
        StructuredRequest(
            prompt=STRUCTURED_PROMPT,
            schema=STRUCTURED_SCHEMA,
            cwd=tmp_path,
            system="Answer with the color red.",
        )
    )
    assert set(result.data) == {"color", "hex"}
    assert isinstance(result.data["hex"], str)
    assert result.model == DEFAULT_MODEL
    assert result.usage is not None
    assert result.usage.input_tokens > 0
    assert result.usage.output_tokens > 0
    assert adapter.stats.unhandled == 0


@pytest.mark.harness
def test_structured_flag_still_exists_in_the_installed_cli() -> None:
    """Cheap and unpaid: `--output-schema` disappearing is the silent breakage."""
    if shutil.which(CLI_COMMAND) is None:
        pytest.skip("codex is not installed")
    output = subprocess.run(
        [CLI_COMMAND, "exec", "--help"], capture_output=True, text=True, timeout=60
    ).stdout
    assert "--output-schema" in output
    assert "--ephemeral" in output
    assert "--skip-git-repo-check" in output
    # Still no way to set a system prompt, which is why one is folded in.
    assert "--system-prompt" not in output
