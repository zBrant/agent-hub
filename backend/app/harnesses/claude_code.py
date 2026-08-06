"""Claude Code adapter — Channel A (``--output-format stream-json``).

**Tested against CLI versions 2.1.222 and 2.1.223**, macOS, subscription auth.
Every statement below was read off the real captures in
``tests/fixtures/claude-code/`` (activity A3, notes in ``NOTES.md`` there); none
of it comes from documentation. When a future release changes the format, the
golden tests in ``tests/harnesses/test_claude_code.py`` fail first.

Two versions, not one: the CLI auto-updated part-way through the A3 capture
session. ``system/init.claude_code_version`` says 2.1.222 in ``simple_edit``,
``tool_error``, ``permission_denied`` and ``multi_turn``, and 2.1.223 in
``budget_error``, ``interrupted`` and ``partial_messages`` (``NOTES.md`` records
2.1.222 for all seven). The line shapes are identical across the two, which is
itself a small piece of evidence that patch releases do not move the format —
but it is why the version here is a set.

Launch flags that are load-bearing
----------------------------------

``-p --output-format stream-json --verbose``
    ``--verbose`` is not optional: ``stream-json`` requires it.
``--permission-mode bypassPermissions``
    **Required for non-interactive tool use to succeed at all.** Without it
    ``permissionMode`` is ``"default"`` and every write is refused — silently,
    see ``permission_denials`` below. The narrower flag is enough;
    ``--dangerously-skip-permissions`` is not needed. Safety comes from the
    sandbox, not from this flag (invariant 8).
``--input-format stream-json``
    Only when injecting messages mid-session (Phase 1). Phase 0 writes the
    prompt to stdin as plain text and closes it, which ends the process cleanly
    with exit 0.
``--include-partial-messages``
    Phase 1. It is the only way to get true incremental text *and* true output
    token counts, at the cost of ~3x the line volume.

Translation table (line shape → event)
--------------------------------------

=================================  =================================================
``system``/``init``                ``TurnStarted``; ``RunStarted`` too on the first
``assistant`` block ``text``       ``AssistantText``
``assistant`` block ``thinking``   ``ThinkingDelta`` (``signature`` dropped)
``assistant`` block ``tool_use``   ``ToolCall``
``user`` block ``tool_result``     ``ToolResult``
``result`` (any subtype)           ``Usage`` then ``TurnFinished``
process exit                       ``RunFinished`` (synthesized, not parsed)
=================================  =================================================

Everything else is in :data:`IGNORED_LINES` with a reason. There is no
``case _: pass`` — an unrecognized line is counted in
:class:`~app.harnesses.base.ParseStats` and logged at warning level.

The three traps this parser exists to avoid
-------------------------------------------

1. **One API message is split across several ``assistant`` lines, one per
   content block, each repeating the identical ``message.usage``.**
   ``simple_edit.ndjson`` has 5 ``assistant`` lines and 3 distinct
   ``message.id``; naively summing ``input_tokens`` over lines gives 37, the
   truth is 21.

2. **``output_tokens`` on an ``assistant`` line is the ``message_start``
   placeholder, ~50x low.** Same fixture: 5 against a real 254. The true value
   exists only in ``stream_event/message_delta`` (needs
   ``--include-partial-messages``) or in the per-turn ``result``.

   Consequence, and the central decision of this module: **``Usage`` is emitted
   from ``result.usage`` only, never from an ``assistant`` line.** One ``Usage``
   per turn, all four fields correct, no dedupe logic to get wrong. The price is
   that token counts land at turn boundaries rather than continuously; live
   incremental display is a Phase 1 concern and needs
   ``--include-partial-messages``.

3. **``result.usage`` is per turn and resets; ``result.modelUsage`` and
   ``result.total_cost_usd`` are cumulative across the session.** Summing
   ``total_cost_usd`` over ``result`` lines double-counts. Neither is emitted
   here — cost is computed at ingest from ``Usage`` (invariant 3).

   One capture contradicts the convenience of trap 2's fix:
   ``budget_error.ndjson`` reports ``result.usage`` **all zeros** while
   ``modelUsage`` shows 10 in / 202 out / 5472 cache-read / 3975 cache-write and
   ``total_cost_usd`` 0.0101612. A turn killed by ``--max-budget-usd`` therefore
   contributes a zero ``Usage``. The parser emits it anyway (the harness said
   zero; inventing a number is worse) but counts it in
   ``ParseStats.zero_usage_turns`` and logs ``harness.zero_usage`` so it cannot
   pass unnoticed.

A permission-blocked run looks exactly like a successful one
------------------------------------------------------------

In ``-p`` mode Claude Code never emits a permission request. It refuses
silently, the refusal arrives as an ordinary ``is_error`` tool result marked
``tool_result_meta[].non_execution_kind == "user-rejected"``, and the run then
reports ``subtype: "success"``, ``is_error: false``, exit code 0.
``result.permission_denials[]`` is the only signal — it is put on
``TurnFinished`` and read through ``TurnFinished.blocked_by_permission``.

Interrupting (Phase 1, but discovered in A3 so it is written down)
------------------------------------------------------------------

``interrupt()`` should **not** be ``os.killpg``. With
``--input-format stream-json`` the CLI accepts on stdin::

    {"type":"control_request","request_id":"req_1","request":{"subtype":"interrupt"}}

and answers ``{"type":"control_response","response":{"subtype":"success",
"request_id":"req_1","response":{"still_queued":[]}}}``, then emits a ``user``
line ``"[Request interrupted by user]"`` and a proper
``result``/``error_during_execution`` with ``terminal_reason:
"aborted_streaming"`` and correct final usage, before exiting 1. A signal gets
none of that. The capability is advertised in
``system/init.capabilities`` as ``interrupt_receipt_v1``, so it is
feature-detectable rather than assumed.
"""

import asyncio
import contextlib
import json
import os
import signal
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from app.harnesses.base import (
    HarnessError,
    ParseStats,
    RunHandle,
    RunSpec,
)
from app.harnesses.events import (
    AgentEvent,
    AssistantText,
    PermissionDenial,
    RunFinished,
    RunStarted,
    RunStatus,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnFinished,
    TurnStarted,
    Usage,
)
from app.models.clock import now_ms
from app.models.ids import RunId

log = structlog.get_logger()

HARNESS_NAME = "claude-code"
CLI_COMMAND = "claude"

# Every version the fixtures were captured from; the CLI updated mid-capture.
TESTED_CLI_VERSIONS: tuple[str, ...] = ("2.1.222", "2.1.223")
TESTED_CLI_VERSION = TESTED_CLI_VERSIONS[-1]

# Model catalog for this harness, keyed the way `pricing.yaml` and design.md §4
# spell them. Claude Code also accepts dated ids and aliases ("opus", "sonnet");
# those are the caller's problem, not a reason to widen this list.
SUPPORTED_MODELS: tuple[str, ...] = (
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)

# Line shapes we drop on purpose. Key is "type" or "type/subtype".
IGNORED_LINES: dict[str, str] = {
    "system/thinking_tokens": (
        "Client-side estimate streamed while the model thinks "
        "(estimated_tokens / estimated_tokens_delta). Not billing data — adding "
        "it to a token total inflates it with a number the API never charged. "
        "The real thinking count is output_tokens_details.thinking_tokens in "
        "message_delta, and it is already inside output_tokens."
    ),
    "system/status": (
        'Spinner state ("requesting"). Pure UI affordance with no state we '
        "track; a node's status comes from TurnStarted/TurnFinished."
    ),
    "rate_limit_event": (
        "Anthropic subscription window: resetsAt, rateLimitType, overageStatus. "
        "Genuinely interesting for a dashboard, but it is provider-specific and "
        "exposing it would put an Anthropic concept in a shared event "
        "(invariant 1). Revisit only as a generalized 'provider quota' event."
    ),
    "control_response": (
        "Acknowledgement of a control_request *we* sent. It belongs to the "
        "request/response bookkeeping of interrupt(), not to the run's history. "
        "Phase 1 correlates it by request_id inside the adapter."
    ),
    "stream_event": (
        "Raw Anthropic SSE, only present with --include-partial-messages. Every "
        "content block it carries is re-delivered whole on an assistant line, "
        "so parsing both would duplicate all text. Phase 1 flips this: consume "
        "stream_event for live deltas and true per-message output tokens, and "
        "ignore the assistant lines instead."
    ),
    # A 'user' line is not always a tool result. Injected prompts and synthetic
    # notices ("[Request interrupted by user]") arrive as user/text blocks; the
    # interruption is already reported as TurnFinished(status="interrupted").
    "user/text": (
        "Synthetic or injected user text; carries no state we do not already have."
    ),
}

# Field-level drops, so they are a decision rather than an oversight:
#   assistant  -> parent_tool_use_id (sub-agent routing, no sub-agents in Phase 0),
#                 request_id, aborted, message.stop_reason/stop_details,
#                 message.diagnostics.cache_miss_reason, message.context_management
#   thinking   -> signature (opaque API re-submission blob, large, meaningless here)
#   tool_use   -> caller ({"type":"direct"})
#   user       -> tool_use_result (polymorphic: structured dict on success, plain
#                 string on failure; the human-readable form is already in the
#                 tool_result block we use for `preview`)
#   result     -> total_cost_usd + modelUsage (cumulative, see module docstring),
#                 num_turns, ttft_ms, ttft_stream_ms, time_to_request_ms,
#                 api_error_status, fast_mode_state, usage.iterations,
#                 usage.service_tier, usage.inference_geo, usage.speed,
#                 usage.server_tool_use (billed as requests, not tokens)
#   init       -> the machine's config inventory: tools, skills, slash_commands,
#                 agents, plugins, mcp_servers, output_style, memory_paths

# Tool output can be a whole file. Keep the event small enough to stream and to
# store; the full text is reproducible from the worktree, the preview is not
# meant to be.
PREVIEW_CHARS = 400

# asyncio's default StreamReader limit is 64 KiB per line, and a single
# `assistant` line carrying a Write tool_use with a large file body exceeds that
# — the read then raises instead of returning the line. Channel A is
# line-delimited JSON, so one oversized line is one lost event.
STREAM_LIMIT = 8 * 1024 * 1024


@dataclass
class _StreamState:
    """Carried across lines of one run. Turn numbering and model spellings."""

    run_id: RunId
    turn: int = 0
    run_started: bool = False
    session_id: str | None = None
    # From `system/init`: the alias, e.g. "claude-haiku-4-5".
    init_model: str | None = None
    # From `assistant` lines: the dated id, e.g. "claude-haiku-4-5-20251001".
    dated_model: str | None = None
    last_status: RunStatus | None = None
    last_summary: str | None = None


def cli_args(spec: RunSpec) -> list[str]:
    """The argv that follows ``claude``.

    The prompt is **not** here: it goes on stdin (`docs/conventions.md` §6).
    """
    args = [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "bypassPermissions",
    ]
    if spec.model is not None:
        args += ["--model", spec.model]
    if spec.max_budget_usd is not None:
        args += ["--max-budget-usd", str(spec.max_budget_usd)]
    return args


def build_argv(spec: RunSpec) -> list[str]:
    """Full argv: sandbox prefix, then ``claude``, then our flags."""
    return [*spec.launcher, CLI_COMMAND, *cli_args(spec)]


def parse_stream(
    source: str | Iterable[str],
    *,
    run_id: RunId,
    clock: Callable[[], int] = now_ms,
    stats: ParseStats | None = None,
) -> Iterator[AgentEvent]:
    """Translate a recorded ``stream-json`` stream into events. Pure, no I/O.

    Used by the golden tests and by replay. It deliberately does **not**
    synthesize a trailing ``RunFinished``: that event needs the process exit
    code, which exists nowhere in the JSON, and inventing one here would make a
    replayed run claim an exit status it never had.
    """
    state = _StreamState(run_id=run_id)
    counters = stats if stats is not None else ParseStats()
    lines: Iterable[str] = source.splitlines() if isinstance(source, str) else source
    for raw in lines:
        yield from _translate_line(raw, state, counters, clock)


def _translate_line(
    raw: str,
    state: _StreamState,
    stats: ParseStats,
    clock: Callable[[], int],
) -> Iterator[AgentEvent]:
    line = raw.strip()
    if not line:
        return
    stats.lines += 1
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        stats.malformed += 1
        log.warning(
            "harness.malformed_line",
            harness=HARNESS_NAME,
            run_id=state.run_id,
            length=len(line),
        )
        return
    if not isinstance(payload, dict):
        stats.malformed += 1
        log.warning(
            "harness.malformed_line",
            harness=HARNESS_NAME,
            run_id=state.run_id,
            json_type=type(payload).__name__,
        )
        return

    events = list(_translate_payload(payload, state, stats, clock))
    stats.events += len(events)
    yield from events


def _translate_payload(
    payload: dict[str, Any],
    state: _StreamState,
    stats: ParseStats,
    clock: Callable[[], int],
) -> Iterator[AgentEvent]:
    key = _line_key(payload)
    if key in IGNORED_LINES:
        stats.count_ignored(key)
        return

    ts = _line_ts(payload, clock)
    match payload.get("type"):
        case "system" if payload.get("subtype") == "init":
            yield from _on_init(payload, state, ts)
        case "assistant":
            yield from _on_assistant(payload, state, stats, ts)
        case "user":
            yield from _on_user(payload, state, stats, ts)
        case "result":
            yield from _on_result(payload, state, stats, ts)
        case _:
            stats.count_unknown(key)
            log.warning(
                "harness.unknown_line",
                harness=HARNESS_NAME,
                run_id=state.run_id,
                line_type=key,
                tested_version=TESTED_CLI_VERSION,
            )


def _on_init(
    payload: dict[str, Any], state: _StreamState, ts: int
) -> Iterator[AgentEvent]:
    state.turn += 1
    state.session_id = _as_str(payload.get("session_id"))
    state.init_model = _as_str(payload.get("model")) or state.init_model
    model = state.init_model or ""
    if not state.run_started:
        state.run_started = True
        yield RunStarted(
            run_id=state.run_id,
            ts=ts,
            harness=HARNESS_NAME,
            model=model,
            cwd=Path(_as_str(payload.get("cwd")) or "."),
            session_id=state.session_id,
            harness_version=_as_str(payload.get("claude_code_version")),
        )
    yield TurnStarted(
        run_id=state.run_id,
        ts=ts,
        turn=state.turn,
        model=model,
        session_id=state.session_id,
    )


def _on_assistant(
    payload: dict[str, Any], state: _StreamState, stats: ParseStats, ts: int
) -> Iterator[AgentEvent]:
    message = payload.get("message")
    if not isinstance(message, dict):
        stats.count_unknown("assistant/no-message")
        return
    state.dated_model = _as_str(message.get("model")) or state.dated_model
    for block in _blocks(message):
        match block.get("type"):
            case "text":
                yield AssistantText(
                    run_id=state.run_id, ts=ts, text=_as_str(block.get("text")) or ""
                )
            case "thinking":
                yield ThinkingDelta(
                    run_id=state.run_id,
                    ts=ts,
                    text=_as_str(block.get("thinking")) or "",
                )
            case "tool_use":
                raw_input = block.get("input")
                yield ToolCall(
                    run_id=state.run_id,
                    ts=ts,
                    call_id=_as_str(block.get("id")) or "",
                    tool=_as_str(block.get("name")) or "",
                    input=raw_input if isinstance(raw_input, dict) else {},
                )
            case other:
                stats.count_unknown(f"assistant/block/{other}")
                log.warning(
                    "harness.unknown_content_block",
                    harness=HARNESS_NAME,
                    run_id=state.run_id,
                    block_type=other,
                )


def _on_user(
    payload: dict[str, Any], state: _StreamState, stats: ParseStats, ts: int
) -> Iterator[AgentEvent]:
    message = payload.get("message")
    if not isinstance(message, dict):
        stats.count_unknown("user/no-message")
        return
    denied_ids = _denied_call_ids(payload)
    for block in _blocks(message):
        match block.get("type"):
            case "tool_result":
                call_id = _as_str(block.get("tool_use_id")) or ""
                yield ToolResult(
                    run_id=state.run_id,
                    ts=ts,
                    call_id=call_id,
                    # There is no `is_error` key at all on success, so the test
                    # has to default to False rather than read a boolean.
                    ok=not bool(block.get("is_error", False)),
                    preview=_preview(block.get("content")),
                    denied=call_id in denied_ids,
                )
            case "text":
                stats.count_ignored("user/text")
            case other:
                stats.count_unknown(f"user/block/{other}")
                log.warning(
                    "harness.unknown_content_block",
                    harness=HARNESS_NAME,
                    run_id=state.run_id,
                    block_type=other,
                )


def _on_result(
    payload: dict[str, Any], state: _StreamState, stats: ParseStats, ts: int
) -> Iterator[AgentEvent]:
    if state.turn == 0:
        # A `result` with no preceding `init`: only possible on a truncated
        # capture or a resumed session. Number the turn anyway rather than
        # emit `turn=0` and break the ge=1 contract.
        state.turn = 1
    usage_raw = payload.get("usage")
    usage = usage_raw if isinstance(usage_raw, dict) else {}
    tiers_raw = usage.get("cache_creation")
    tiers = tiers_raw if isinstance(tiers_raw, dict) else {}

    event = Usage(
        run_id=state.run_id,
        ts=ts,
        # Prefer the dated id: it is what the API billed against, and pricing
        # normalizes aliases anyway. Fall back to init's alias when a turn
        # produced no assistant line at all.
        model=state.dated_model or state.init_model or "",
        input_tokens=_as_int(usage.get("input_tokens")),
        output_tokens=_as_int(usage.get("output_tokens")),
        cache_read_tokens=_as_int(usage.get("cache_read_input_tokens")),
        cache_write_tokens=_as_int(usage.get("cache_creation_input_tokens")),
        cache_write_5m_tokens=_as_int(tiers.get("ephemeral_5m_input_tokens")),
        cache_write_1h_tokens=_as_int(tiers.get("ephemeral_1h_input_tokens")),
    )
    if event.total_tokens == 0 and _model_usage_total(payload) > 0:
        stats.zero_usage_turns += 1
        log.warning(
            "harness.zero_usage",
            harness=HARNESS_NAME,
            run_id=state.run_id,
            turn=state.turn,
            subtype=payload.get("subtype"),
            model_usage_tokens=_model_usage_total(payload),
        )
    yield event

    status = _turn_status(payload)
    state.last_status = status
    state.last_summary = _as_str(payload.get("result"))
    yield TurnFinished(
        run_id=state.run_id,
        ts=ts,
        turn=state.turn,
        status=status,
        summary=state.last_summary,
        permission_denials=_denials(payload),
        errors=tuple(
            str(item) for item in _as_list(payload.get("errors")) if item is not None
        ),
        session_id=state.session_id,
        duration_ms=_as_int(payload.get("duration_ms")) or None,
    )


def _turn_status(payload: dict[str, Any]) -> RunStatus:
    """`result.subtype` + `terminal_reason` → a harness-neutral status.

    Note what is *not* here: a turn with non-empty ``permission_denials`` still
    reports ``success``, because that is what the harness reports and the parser
    does not invent verdicts. ``TurnFinished.blocked_by_permission`` is the
    signal the scheduler must check.
    """
    subtype = payload.get("subtype")
    if subtype == "success":
        return "success"
    if subtype == "error_max_budget_usd":
        return "budget_exceeded"
    if payload.get("terminal_reason") in {
        "aborted_streaming",
        "aborted",
        "interrupted",
    }:
        return "interrupted"
    return "failed"


def run_status(exit_code: int | None, last_turn: RunStatus | None) -> RunStatus:
    """Status for the synthesized ``RunFinished``.

    A non-success last turn wins: it says *why* (interrupted, budget), which
    exit code 1 alone does not. Otherwise the exit code decides — a turn that
    reported success while the process died non-zero is a failure.
    """
    if last_turn is not None and last_turn != "success":
        return last_turn
    if exit_code == 0:
        return "success"
    return "failed"


def _line_key(payload: dict[str, Any]) -> str:
    line_type = _as_str(payload.get("type")) or "<missing>"
    subtype = _as_str(payload.get("subtype"))
    return f"{line_type}/{subtype}" if subtype else line_type


def _line_ts(payload: dict[str, Any], clock: Callable[[], int]) -> int:
    """The harness's own stamp when it has one, our ingest clock otherwise.

    `assistant` and `user` lines carry an ISO-8601 `timestamp`; `system/init`
    and `result` do not. Same machine, so the two clocks do not meaningfully
    diverge, and using the harness stamp keeps the recorded time of a message
    rather than the time we got around to reading it.
    """
    raw = payload.get("timestamp")
    if isinstance(raw, str):
        try:
            return int(datetime.fromisoformat(raw).timestamp() * 1000)
        except ValueError:
            log.warning("harness.bad_timestamp", harness=HARNESS_NAME, value=raw)
    return clock()


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _denied_call_ids(payload: dict[str, Any]) -> frozenset[str]:
    """Tool calls the permission layer refused rather than ran.

    ``non_execution_kind == "user-rejected"`` is the only thing separating a
    refusal from a genuine tool failure — both arrive with ``is_error: true``.
    """
    denied = set()
    for meta in _as_list(payload.get("tool_result_meta")):
        if isinstance(meta, dict) and meta.get("non_execution_kind") == "user-rejected":
            call_id = _as_str(meta.get("id"))
            if call_id:
                denied.add(call_id)
    return frozenset(denied)


def _denials(payload: dict[str, Any]) -> tuple[PermissionDenial, ...]:
    out = []
    for item in _as_list(payload.get("permission_denials")):
        if not isinstance(item, dict):
            continue
        tool_input = item.get("tool_input")
        out.append(
            PermissionDenial(
                tool=_as_str(item.get("tool_name")) or "",
                call_id=_as_str(item.get("tool_use_id")),
                input=tool_input if isinstance(tool_input, dict) else {},
            )
        )
    return tuple(out)


def _model_usage_total(payload: dict[str, Any]) -> int:
    """Tokens in ``result.modelUsage``, used only as a sanity check.

    Cumulative across the session and includes a side-channel model that no
    event ever reports, so it must never feed a ``Usage``. It is enough to tell
    "this turn really did consume tokens" when ``result.usage`` says zero.
    """
    model_usage = payload.get("modelUsage")
    if not isinstance(model_usage, dict):
        return 0
    total = 0
    for entry in model_usage.values():
        if not isinstance(entry, dict):
            continue
        total += (
            _as_int(entry.get("inputTokens"))
            + _as_int(entry.get("outputTokens"))
            + _as_int(entry.get("cacheReadInputTokens"))
            + _as_int(entry.get("cacheCreationInputTokens"))
        )
    return total


def _preview(content: object) -> str:
    """Tool result content is polymorphic: a string, or a list of blocks."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    elif content is None:
        text = ""
    else:
        text = json.dumps(content, sort_keys=True)
    if len(text) <= PREVIEW_CHARS:
        return text
    return text[:PREVIEW_CHARS] + "…"


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


class ClaudeCodeAdapter:
    """`design.md` §3 adapter for Claude Code. Channel A only in Phase 0."""

    name: str
    supported_models: list[str]

    def __init__(self) -> None:
        self.name = HARNESS_NAME
        self.supported_models = list(SUPPORTED_MODELS)

    async def start(self, spec: RunSpec) -> RunHandle:
        argv = build_argv(spec)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=dict(spec.env) if spec.env else None,
            # Own process group, so killing the run takes down the whole tree —
            # claude spawns bash, which spawns whatever the agent asked for.
            start_new_session=True,
            limit=STREAM_LIMIT,
        )
        handle = RunHandle(
            run_id=spec.run_id,
            argv=tuple(argv),
            process=process,
            started_ms=now_ms(),
        )
        log.info(
            "harness.started",
            harness=HARNESS_NAME,
            run_id=spec.run_id,
            pid=handle.pid,
            model=spec.model,
            # Never the prompt itself (docs/conventions.md §2).
            prompt_chars=len(spec.prompt),
        )
        # Both as tasks: writing the prompt inline can deadlock against a child
        # that fills the stdout pipe before it finishes reading stdin, and an
        # undrained stderr pipe blocks the child at 64 KiB of banner or warnings.
        handle.tasks.append(
            asyncio.create_task(_write_prompt(handle, spec.prompt), name="claude-stdin")
        )
        handle.tasks.append(
            asyncio.create_task(_drain_stderr(handle), name="claude-stderr")
        )
        return handle

    async def send(self, handle: RunHandle, text: str) -> None:
        raise NotImplementedError(
            "Phase 1: requires --input-format stream-json and one JSON object "
            'per line on stdin: {"type":"user","message":{"role":"user",'
            '"content":[{"type":"text","text":"…"}]}}. Verified in A3 '
            "(multi_turn.ndjson): writing it after a `result` line starts a new "
            "turn with a fresh system/init and the same session_id."
        )

    async def interrupt(self, handle: RunHandle) -> None:
        raise NotImplementedError(
            "Phase 1: send a control_request on stdin rather than a signal — see "
            "the module docstring. A signal loses the terminal `result` line and "
            "with it the turn's final usage."
        )

    async def kill(self, handle: RunHandle) -> None:
        process = handle.process
        if process.returncode is not None:
            return
        _signal_group(handle, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            log.warning(
                "harness.kill_escalated", harness=HARNESS_NAME, run_id=handle.run_id
            )
            _signal_group(handle, signal.SIGKILL)
            await process.wait()
        await self._stop_tasks(handle)

    async def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]:
        stdout = handle.process.stdout
        if stdout is None:
            raise HarnessError(
                f"run {handle.run_id}: no stdout pipe; argv={list(handle.argv)}"
            )
        state = _StreamState(run_id=handle.run_id)
        stats = ParseStats()
        try:
            async for raw in stdout:
                for event in _translate_line(
                    raw.decode("utf-8", "replace"), state, stats, now_ms
                ):
                    yield event
            exit_code = await handle.process.wait()
        except asyncio.CancelledError:
            # No await here: the task is already cancelled, so anything we wait
            # on raises immediately. SIGKILL to the group is the only teardown
            # guaranteed to complete synchronously.
            _signal_group(handle, signal.SIGKILL)
            raise
        finally:
            for task in handle.tasks:
                task.cancel()

        if stats.unhandled:
            log.warning(
                "harness.unhandled_lines",
                harness=HARNESS_NAME,
                run_id=handle.run_id,
                unknown=stats.unknown,
                malformed=stats.malformed,
                tested_version=TESTED_CLI_VERSION,
            )
        if exit_code != 0 and handle.stderr_tail:
            log.warning(
                "harness.exited_nonzero",
                harness=HARNESS_NAME,
                run_id=handle.run_id,
                exit_code=exit_code,
                stderr_tail=list(handle.stderr_tail),
            )
        yield RunFinished(
            run_id=handle.run_id,
            ts=now_ms(),
            status=run_status(exit_code, state.last_status),
            exit_code=exit_code,
            summary=state.last_summary,
        )

    @staticmethod
    async def _stop_tasks(handle: RunHandle) -> None:
        for task in handle.tasks:
            task.cancel()
        for task in handle.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        handle.tasks.clear()


async def _write_prompt(handle: RunHandle, prompt: str) -> None:
    stdin = handle.process.stdin
    if stdin is None:
        return
    try:
        stdin.write(prompt.encode())
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The child exited before reading the prompt; the exit code and the
        # stderr tail explain why, and RunFinished will carry them.
        log.warning("harness.stdin_closed", harness=HARNESS_NAME, run_id=handle.run_id)
    finally:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            stdin.close()


async def _drain_stderr(handle: RunHandle) -> None:
    stderr = handle.process.stderr
    if stderr is None:
        return
    async for raw in stderr:
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            handle.stderr_tail.append(line)


def _signal_group(handle: RunHandle, sig: signal.Signals) -> None:
    try:
        os.killpg(os.getpgid(handle.pid), sig)
    except (ProcessLookupError, PermissionError):
        # Already reaped, or the group is not ours because start_new_session
        # did not apply. Fall back to the single process.
        with contextlib.suppress(ProcessLookupError):
            handle.process.kill()
