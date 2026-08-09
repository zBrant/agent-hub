"""Codex CLI adapter — Channel A (``codex exec --json``).

**Tested against codex-cli 0.146.0**, macOS, ChatGPT authentication. Every JSON
shape below appears in a real sanitized capture under ``tests/fixtures/codex``.
The official CLI manual establishes ``exec --json`` as the stable
non-interactive JSONL surface; the fixtures establish what this installed
version actually emits.

Launch decisions
----------------

``exec --json``
    Makes stdout a JSONL event stream. Diagnostics stay on stderr.
``--ignore-user-config --ignore-rules``
    Keeps a run deterministic and prevents user/project exec-policy from
    weakening or blocking the orchestration policy. Authentication is still
    loaded from ``CODEX_HOME``.
``--dangerously-bypass-approvals-and-sandbox``
    Used only when :class:`RunSpec` already carries the externally hardened
    ai-jail launcher. Nested sandboxes fail on macOS and approvals cannot be
    answered in non-interactive mode. Without a launcher, contract tests use
    Codex's own ``workspace-write`` sandbox instead.
``-``
    Reads the prompt from stdin. Prompts never enter argv (conventions §6).

Translation table (line shape → event)
--------------------------------------

========================================  =====================================
``thread.started``                        ``RunStarted`` (first one only)
``turn.started``                          ``TurnStarted``
``item.completed`` / ``agent_message``    ``AssistantText``
``item.completed`` / ``reasoning``        ``ThinkingDelta``
``item.started`` / tool-like item          ``ToolCall``
``item.completed`` / tool-like item        ``ToolResult``
``turn.completed``                         ``Usage`` then ``TurnFinished``
``turn.failed``                            ``TurnFinished(status="failed")``
process exit                               ``RunFinished`` (synthesized)
========================================  =====================================

Token semantics
---------------

``turn.completed.usage`` is per-turn. ``input_tokens`` includes cached input,
and ``output_tokens`` includes reasoning output: both detailed fields are
breakdowns, not additions. AgentHub's four fields are mutually exclusive, so
the mapping is ``input = total_input - cached_input - cache_write``,
``cache_read = cached_input``, ``cache_write = cache_write_input_tokens``, and
``output = output_tokens``. A breakdown larger than its total is rejected as
unreconciled rather than poisoning an append-only aggregate.

Interruption
------------

Ctrl-C exits 1 without a terminal JSON event. :meth:`interrupt` records intent
on the handle before signaling the process group; :meth:`events` can therefore
distinguish an operator interruption from an ordinary non-zero process exit.

Structured output (``--output-schema``), verified on 0.146.0
-------------------------------------------------------------

``codex exec --help`` describes ``--output-schema <FILE>`` as "Path to a JSON
Schema file describing the model's final response shape". Three things the help
text does not say, all of them established by running it:

1. **There is no structured field.** Claude Code answers a schema with a
   dedicated ``structured_output`` object; Codex does not. The stream is the
   ordinary one — ``thread.started``, ``turn.started``,
   ``item.completed``/``agent_message``, ``turn.completed`` — and the schema's
   payload is the *text of the final agent message*, raw JSON with no fence and
   no prose. :func:`structured_result` therefore takes the last
   :class:`~app.harnesses.events.AssistantText` and parses it, and "missing
   structured field" means "the run produced no agent message at all".

2. **The schema must be OpenAI-strict, and the CLI does not make it so.** A
   schema without ``"additionalProperties": false`` on *every* object is
   rejected by the provider, exit 1, with
   ``invalid_json_schema: ... 'additionalProperties' is required to be supplied
   and to be false``; a ``required`` array that does not list every key in
   ``properties`` is rejected the same way. Both were reproduced here. This
   adapter passes the schema through byte for byte — rewriting a caller's schema
   would silently change what "valid" means, and making optional fields
   mandatory changes the answer. A caller that must work on both harnesses has
   to emit strict schemas, exactly as it already has to resolve ``$ref``
   (`base.py`, :class:`~app.harnesses.base.StructuredRequest`). Claude Code
   accepts strict schemas too, so this is a tightening, not a fork.

3. **There is no system-prompt flag.** ``codex exec`` has no
   ``--system-prompt``/``--instructions`` of any kind; the only instruction
   channels are the prompt and ``AGENTS.md``, and writing an ``AGENTS.md`` would
   mean writing into the caller's directory. :func:`structured_prompt` folds
   ``system`` into the stdin prompt under a heading instead.

Everything else follows the run path: the prompt goes to stdin via ``-``, the
schema file is written to a 0700 temp directory and deleted in a ``finally``,
and ``--sandbox read-only`` replaces ``workspace-write`` because a
schema-constrained answer has nothing to write.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator, Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from app.harnesses.base import (
    STDERR_TAIL_LINES,
    HarnessError,
    ParseStats,
    RunHandle,
    RunSpec,
    StructuredRequest,
    StructuredResult,
)
from app.harnesses.events import (
    AgentEvent,
    AssistantText,
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

HARNESS_NAME = "codex"
CLI_COMMAND = "codex"
TESTED_CLI_VERSION = "0.146.0"
DEFAULT_MODEL = "gpt-5.6-sol"
SUPPORTED_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
STREAM_LIMIT = 8 * 1024 * 1024
PREVIEW_CHARS = 400

# `StructuredRequest` deliberately carries no run_id — a schema-constrained
# answer is not a node of the graph, and giving it one would let a `usage_event`
# row be written for something that never ran (base.py). `Usage` still requires
# the field, so this sentinel states that the tokens belong to no run.
STRUCTURED_RUN_ID: RunId = "run_structured"

# A provider rejection (a 400 on the schema, say) is reported twice: once as an
# `error` line and once inside `turn.failed`. The text is a whole nested JSON
# document, so the HarnessError quotes it bounded rather than whole.
ERROR_DETAIL_CHARS = 500

TOOL_ITEM_TYPES = frozenset(
    {"command_execution", "file_change", "mcp_tool_call", "web_search"}
)

IGNORED_LINES: dict[str, str] = {
    "thread.started/repeated": (
        "codex exec resume starts a new process and repeats thread.started with "
        "the same id; one AgentHub run still has exactly one RunStarted"
    ),
    "item.started/agent_message": (
        "an agent message has no useful payload until item.completed; emitting "
        "both would duplicate the visible text"
    ),
    "item.started/reasoning": (
        "reasoning has no useful payload until item.completed; emitting both "
        "would duplicate the reasoning text"
    ),
}


@dataclass
class _StreamState:
    run_id: RunId
    model: str
    cwd: Path
    pid: int | None = None
    thread_id: str | None = None
    run_started: bool = False
    turn: int = 0
    last_status: RunStatus | None = None
    last_summary: str | None = None
    pending_errors: list[str] = field(default_factory=list)
    started_tools: set[str] = field(default_factory=set)


def build_argv(spec: RunSpec) -> list[str]:
    """Build a deterministic non-interactive invocation; prompt stays on stdin."""
    if spec.max_budget_usd is not None:
        raise ValueError("Codex CLI has no --max-budget-usd equivalent")
    model = spec.model or DEFAULT_MODEL
    if model not in SUPPORTED_MODELS:
        raise ValueError(
            f"unsupported Codex model {model!r}; expected one of {SUPPORTED_MODELS!r}"
        )

    argv = [
        *spec.launcher,
        CLI_COMMAND,
        "exec",
        "--json",
        "--color",
        "never",
        "--ignore-user-config",
        "--ignore-rules",
        "--model",
        model,
        "--cd",
        str(spec.cwd),
    ]
    if spec.launcher:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        argv += ["--sandbox", "workspace-write"]
    argv.append("-")
    return argv


def parse_stream(
    lines: str | Iterable[str],
    *,
    run_id: RunId,
    model: str = DEFAULT_MODEL,
    cwd: Path = Path("/tmp/repo"),
    clock: Callable[[], int] = now_ms,
    stats: ParseStats | None = None,
) -> Iterator[AgentEvent]:
    """Translate a recorded JSONL stream without synthesizing process exit."""
    counters = stats if stats is not None else ParseStats()
    state = _StreamState(run_id=run_id, model=model, cwd=cwd)
    source = lines.splitlines() if isinstance(lines, str) else lines
    for raw in source:
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
        log.warning("harness.malformed_line", harness=HARNESS_NAME, preview=line[:200])
        return
    if not isinstance(payload, dict):
        stats.malformed += 1
        return

    events = tuple(_translate_payload(payload, state, stats, clock()))
    stats.events += len(events)
    yield from events


def _translate_payload(
    payload: dict[str, Any],
    state: _StreamState,
    stats: ParseStats,
    ts: int,
) -> Iterator[AgentEvent]:
    event_type = payload.get("type")
    if event_type == "thread.started":
        thread_id = _as_str(payload.get("thread_id"))
        if thread_id is not None:
            state.thread_id = thread_id
        if state.run_started:
            stats.count_ignored("thread.started/repeated")
            return
        state.run_started = True
        yield RunStarted(
            run_id=state.run_id,
            ts=ts,
            harness=HARNESS_NAME,
            model=state.model,
            cwd=state.cwd,
            pid=state.pid,
            session_id=state.thread_id,
            harness_version=TESTED_CLI_VERSION,
        )
        return

    if event_type == "turn.started":
        state.turn += 1
        yield TurnStarted(
            run_id=state.run_id,
            ts=ts,
            turn=state.turn,
            model=state.model,
            session_id=state.thread_id,
        )
        return

    if event_type in {"item.started", "item.completed"}:
        yield from _translate_item(payload, state, stats, ts)
        return

    if event_type == "turn.completed":
        yield from _finish_turn(payload, state, stats, ts, "success")
        return

    if event_type == "turn.failed":
        message = _error_message(payload)
        if message:
            state.pending_errors.append(message)
        yield from _finish_turn(payload, state, stats, ts, "failed")
        return

    if event_type == "error":
        message = _error_message(payload)
        if message:
            state.pending_errors.append(message)
        return

    key = str(event_type) if event_type is not None else "<missing-type>"
    stats.count_unknown(key)
    log.warning("harness.unknown_line", harness=HARNESS_NAME, line_type=key)


def _translate_item(
    payload: dict[str, Any],
    state: _StreamState,
    stats: ParseStats,
    ts: int,
) -> Iterator[AgentEvent]:
    phase = _as_str(payload.get("type")) or "item.unknown"
    item = payload.get("item")
    if not isinstance(item, dict):
        stats.malformed += 1
        return
    item_type = _as_str(item.get("type")) or "<missing-item-type>"
    item_id = _as_str(item.get("id")) or "<missing-item-id>"

    if phase == "item.started":
        if item_type in {"agent_message", "reasoning"}:
            stats.count_ignored(f"item.started/{item_type}")
        elif item_type in TOOL_ITEM_TYPES:
            state.started_tools.add(item_id)
            yield ToolCall(
                run_id=state.run_id,
                ts=ts,
                call_id=item_id,
                tool=_tool_name(item),
                input=_tool_input(item),
            )
        else:
            stats.count_unknown(f"item.started/{item_type}")
        return

    if item_type == "agent_message":
        text = _as_str(item.get("text"))
        if text:
            state.last_summary = text
            yield AssistantText(run_id=state.run_id, ts=ts, text=text)
        return

    if item_type == "reasoning":
        text = _as_str(item.get("text"))
        if text:
            yield ThinkingDelta(run_id=state.run_id, ts=ts, text=text)
        return

    if item_type == "error":
        message = _error_message(item)
        if message:
            state.pending_errors.append(message)
        return

    if item_type in TOOL_ITEM_TYPES:
        if item_id not in state.started_tools:
            yield ToolCall(
                run_id=state.run_id,
                ts=ts,
                call_id=item_id,
                tool=_tool_name(item),
                input=_tool_input(item),
            )
        else:
            state.started_tools.remove(item_id)
        yield ToolResult(
            run_id=state.run_id,
            ts=ts,
            call_id=item_id,
            ok=_tool_succeeded(item),
            preview=_tool_preview(item),
        )
        return

    stats.count_unknown(f"item.completed/{item_type}")


def _finish_turn(
    payload: dict[str, Any],
    state: _StreamState,
    stats: ParseStats,
    ts: int,
    status: RunStatus,
) -> Iterator[AgentEvent]:
    if state.turn == 0:
        state.turn = 1
    usage = payload.get("usage")
    if isinstance(usage, dict):
        total_input = _as_int(usage.get("input_tokens"))
        cache_read = _as_int(usage.get("cached_input_tokens"))
        cache_write = _as_int(usage.get("cache_write_input_tokens"))
        if cache_read + cache_write > total_input:
            stats.usage_unreconciled_turns += 1
            log.warning(
                "harness.usage_unreconciled",
                harness=HARNESS_NAME,
                run_id=state.run_id,
                turn=state.turn,
                input_tokens=total_input,
                cached_input_tokens=cache_read,
                cache_write_input_tokens=cache_write,
            )
        else:
            yield Usage(
                run_id=state.run_id,
                ts=ts,
                model=state.model,
                input_tokens=total_input - cache_read - cache_write,
                output_tokens=_as_int(usage.get("output_tokens")),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )

    errors = tuple(state.pending_errors)
    state.pending_errors.clear()
    state.last_status = status
    yield TurnFinished(
        run_id=state.run_id,
        ts=ts,
        turn=state.turn,
        status=status,
        summary=state.last_summary,
        errors=errors,
        session_id=state.thread_id,
    )


def _tool_name(item: dict[str, Any]) -> str:
    item_type = _as_str(item.get("type")) or "unknown"
    if item_type != "mcp_tool_call":
        return {
            "command_execution": "shell",
            "file_change": "file_change",
            "web_search": "web_search",
        }.get(item_type, item_type)
    server = _as_str(item.get("server"))
    tool = _as_str(item.get("tool")) or "mcp_tool"
    return f"{server}.{tool}" if server else tool


def _tool_input(item: dict[str, Any]) -> dict[str, Any]:
    item_type = item.get("type")
    if item_type == "command_execution":
        return {"command": _as_str(item.get("command")) or ""}
    if item_type == "file_change":
        return {"changes": _as_list(item.get("changes"))}
    if item_type == "web_search":
        return {"query": _as_str(item.get("query")) or ""}
    if item_type == "mcp_tool_call":
        arguments = item.get("arguments")
        return arguments if isinstance(arguments, dict) else {"arguments": arguments}
    return {}


def _tool_succeeded(item: dict[str, Any]) -> bool:
    status = item.get("status")
    exit_code = item.get("exit_code")
    return status in {"completed", "success"} and exit_code in (None, 0)


def _tool_preview(item: dict[str, Any]) -> str:
    if item.get("type") == "command_execution":
        value: object = item.get("aggregated_output") or ""
    elif item.get("type") == "file_change":
        value = item.get("changes") or []
    else:
        value = item.get("result") or item.get("output") or ""
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
    return text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "…"


def _error_message(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if isinstance(error, dict):
        return _as_str(error.get("message")) or _as_str(error.get("code"))
    return _as_str(payload.get("message")) or _as_str(error)


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _as_list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


# ---------------------------------------------------------------------------
# Structured output — see the module docstring's `--output-schema` section.
# ---------------------------------------------------------------------------


def structured_model(model: str | None) -> str:
    """Same catalog check :func:`build_argv` makes, and the same ``ValueError``.

    A model the adapter does not know is a caller mistake, not a harness
    failure, so it is not a :class:`HarnessError`.
    """
    chosen = model or DEFAULT_MODEL
    if chosen not in SUPPORTED_MODELS:
        raise ValueError(
            f"unsupported Codex model {chosen!r}; expected one of {SUPPORTED_MODELS!r}"
        )
    return chosen


def structured_prompt(request: StructuredRequest) -> str:
    """``system`` folded into the stdin prompt, because there is no flag for it.

    Verified on 0.146.0: ``codex exec`` has no system-prompt option at all. The
    heading is what keeps the instruction and the task distinguishable to the
    model — running the two texts together is how a system prompt gets read as
    part of the question.
    """
    if request.system is None:
        return request.prompt
    return f"# Instructions\n\n{request.system}\n\n# Task\n\n{request.prompt}"


def build_structured_argv(
    request: StructuredRequest, *, schema_file: Path
) -> list[str]:
    """Full argv for one schema-constrained answer; prompt stays on stdin.

    ``--ephemeral`` because a planner question is not a session anyone resumes,
    and ``--skip-git-repo-check`` because :class:`StructuredRequest` has no
    worktree — its ``cwd`` is optional and need not be a repository.
    """
    argv = [
        *request.launcher,
        CLI_COMMAND,
        "exec",
        "--json",
        "--color",
        "never",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--ephemeral",
        "--model",
        structured_model(request.model),
        "--output-schema",
        str(schema_file),
    ]
    # Same rule as build_argv: nested sandboxes fail on macOS, so an externally
    # hardened launcher replaces Codex's own (invariant 8).
    if request.launcher:
        argv.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        # read-only, not workspace-write: answering a schema writes nothing.
        argv += ["--sandbox", "read-only"]
    if request.cwd is not None:
        argv += ["--cd", str(request.cwd)]
    argv.append("-")
    return argv


def _sum_usage(usages: list[Usage], model: str) -> Usage | None:
    """One turn's ``Usage``, or the sum when the CLI took several.

    ``turn.completed.usage`` is per turn (module docstring), so turns add. A
    structured answer is one turn in every capture so far; summing costs ten
    lines and removes the assumption.
    """
    if not usages:
        return None
    if len(usages) == 1:
        return usages[0]
    return Usage(
        run_id=STRUCTURED_RUN_ID,
        ts=usages[-1].ts,
        model=model,
        input_tokens=sum(usage.input_tokens for usage in usages),
        output_tokens=sum(usage.output_tokens for usage in usages),
        cache_read_tokens=sum(usage.cache_read_tokens for usage in usages),
        cache_write_tokens=sum(usage.cache_write_tokens for usage in usages),
    )


def _log_discarded_usage(usage: Usage | None) -> None:
    """A structured call that failed still burned tokens.

    They cannot be returned — :class:`StructuredResult` is all-or-nothing — but
    they must not vanish without a trace either.
    """
    if usage is not None and usage.total_tokens:
        log.warning(
            "harness.structured_usage_discarded",
            harness=HARNESS_NAME,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )


def structured_result(
    stdout: str,
    exit_code: int,
    *,
    model: str,
    cwd: Path,
    stats: ParseStats,
) -> StructuredResult:
    """Translate one ``exec --json --output-schema`` stream, or say what broke.

    Pure, so the golden test can drive it without a process. The stream goes
    through :func:`parse_stream` rather than a private reader, which is what
    keeps the four-field accounting single-sourced (invariant 3).

    Every failure names which of the three it was — bad exit, no final message,
    unparseable final message. None of them quotes the prompt (`conventions` §6);
    the provider's own rejection text is quoted bounded, because without it a
    schema 400 is indistinguishable from a network failure.
    """
    events = list(
        parse_stream(
            stdout, run_id=STRUCTURED_RUN_ID, model=model, cwd=cwd, stats=stats
        )
    )
    usage = _sum_usage([e for e in events if isinstance(e, Usage)], model)

    if exit_code != 0:
        _log_discarded_usage(usage)
        raise HarnessError(
            f"codex exec --output-schema exited {exit_code}{_failure_detail(events)}"
        )
    texts = [event.text for event in events if isinstance(event, AssistantText)]
    if not texts:
        _log_discarded_usage(usage)
        raise HarnessError(
            "codex exec --output-schema returned no agent message, so there is "
            f"no structured answer to read{_failure_detail(events)}"
        )
    try:
        data = json.loads(texts[-1])
    except json.JSONDecodeError as exc:
        _log_discarded_usage(usage)
        raise HarnessError(
            "codex exec --output-schema produced an unparseable final message: "
            f"{exc.msg} at character {exc.pos} of {len(texts[-1])}"
        ) from exc
    if not isinstance(data, dict):
        _log_discarded_usage(usage)
        raise HarnessError(
            "codex exec --output-schema produced an unparseable final message: "
            f"expected a JSON object, got {type(data).__name__}"
        )
    return StructuredResult(data=data, usage=usage, model=model)


def _failure_detail(events: list[AgentEvent]) -> str:
    """The harness's own error text, deduplicated and bounded."""
    messages = list(
        dict.fromkeys(
            message
            for event in events
            if isinstance(event, TurnFinished)
            for message in event.errors
        )
    )
    if not messages:
        return ""
    detail = "; ".join(messages)
    if len(detail) > ERROR_DETAIL_CHARS:
        detail = detail[:ERROR_DETAIL_CHARS] + "…"
    return f": {detail}"


async def _write_private_file(name: str, content: str) -> Path:
    """A file the CLI must read from disk. ``mkdtemp`` is 0700.

    Blocking I/O, hence the thread (invariant 5).
    """

    def write() -> Path:
        directory = Path(tempfile.mkdtemp(prefix="agenthub-structured-"))
        path = directory / name
        path.write_text(content, encoding="utf-8")
        return path

    return await asyncio.to_thread(write)


async def _discard_private_file(path: Path) -> None:
    def remove() -> None:
        shutil.rmtree(path.parent, ignore_errors=True)

    await asyncio.to_thread(remove)


async def _run_once(
    argv: list[str], request: StructuredRequest, prompt: str
) -> tuple[str, str, int]:
    """Run to completion with the prompt on stdin. Returns stdout, stderr, exit.

    ``communicate`` rather than a hand-rolled reader: one shot, no streaming, and
    it is the only thing that cannot deadlock between a child filling stdout and
    us filling its stdin.
    """
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=request.cwd,
        env=dict(request.env) if request.env else None,
        start_new_session=True,
        limit=STREAM_LIMIT,
    )
    log.info(
        "harness.structured_started",
        harness=HARNESS_NAME,
        pid=process.pid,
        model=request.model,
        prompt_chars=len(prompt),
    )
    try:
        out, err = await process.communicate(prompt.encode())
    except asyncio.CancelledError:
        _signal_process_group(process, signal.SIGKILL)
        raise
    exit_code = process.returncode if process.returncode is not None else -1
    return (
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
        exit_code,
    )


class CodexAdapter:
    """Adapter for the stable non-interactive Codex JSONL surface."""

    name: str
    supported_models: list[str]
    stats: ParseStats

    def __init__(self) -> None:
        self.name = HARNESS_NAME
        self.supported_models = list(SUPPORTED_MODELS)
        self.stats = ParseStats()

    def build_argv(self, spec: RunSpec) -> list[str]:
        return build_argv(spec)

    async def start(self, spec: RunSpec) -> RunHandle:
        argv = self.build_argv(spec)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=spec.cwd,
            env=dict(spec.env) if spec.env else None,
            start_new_session=True,
            limit=STREAM_LIMIT,
        )
        handle = RunHandle(
            run_id=spec.run_id,
            argv=tuple(argv),
            process=process,
            started_ms=now_ms(),
            model=spec.model or DEFAULT_MODEL,
            cwd=spec.cwd,
        )
        handle.tasks.append(
            asyncio.create_task(_write_prompt(handle, spec.prompt), name="codex-stdin")
        )
        handle.tasks.append(
            asyncio.create_task(_drain_stderr(handle), name="codex-stderr")
        )
        log.info(
            "harness.started",
            harness=HARNESS_NAME,
            run_id=spec.run_id,
            pid=handle.pid,
            model=handle.model,
            prompt_chars=len(spec.prompt),
        )
        return handle

    async def send(self, handle: RunHandle, text: str) -> None:
        raise NotImplementedError(
            "Phase 1 continuation requires launching `codex exec resume "
            "<thread_id> -` as the next process for this run"
        )

    async def interrupt(self, handle: RunHandle) -> None:
        if handle.process.returncode is not None:
            return
        handle.interrupted = True
        _signal_group(handle, signal.SIGINT)

    async def kill(self, handle: RunHandle) -> None:
        process = handle.process
        if process.returncode is not None:
            return
        handle.interrupted = True
        _signal_group(handle, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except TimeoutError:
            _signal_group(handle, signal.SIGKILL)
            await process.wait()
        await self._stop_tasks(handle)

    async def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]:
        stdout = handle.process.stdout
        if stdout is None:
            raise HarnessError(
                f"run {handle.run_id}: no stdout pipe; argv={list(handle.argv)}"
            )
        state = _StreamState(
            run_id=handle.run_id,
            model=handle.model or DEFAULT_MODEL,
            cwd=handle.cwd or Path.cwd(),
            pid=handle.pid,
        )
        self.stats = ParseStats()
        try:
            async for raw in stdout:
                for event in _translate_line(
                    raw.decode("utf-8", "replace"), state, self.stats, now_ms
                ):
                    yield event
            exit_code = await handle.process.wait()
        except asyncio.CancelledError:
            handle.interrupted = True
            _signal_group(handle, signal.SIGKILL)
            raise
        finally:
            for task in handle.tasks:
                task.cancel()

        if self.stats.unhandled:
            log.warning(
                "harness.unhandled_lines",
                harness=HARNESS_NAME,
                run_id=handle.run_id,
                unknown=self.stats.unknown,
                malformed=self.stats.malformed,
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

        if handle.interrupted:
            status: RunStatus = "interrupted"
        elif exit_code == 0 and state.last_status == "success":
            status = "success"
        else:
            status = state.last_status or "failed"
        yield RunFinished(
            run_id=handle.run_id,
            ts=now_ms(),
            status=status,
            exit_code=exit_code,
            summary=state.last_summary,
        )

    async def complete_structured(self, request: StructuredRequest) -> StructuredResult:
        """One schema-constrained answer (:class:`StructuredCompleter`).

        Independent of :meth:`start`: no ``RunHandle``, no ``RunFinished``, and
        no worktree. ``self.stats`` is replaced so a caller can still audit the
        token accounting of the call it just made.
        """
        model = structured_model(request.model)
        prompt = structured_prompt(request)
        schema_file = await _write_private_file(
            "output-schema.json", json.dumps(dict(request.schema), sort_keys=True)
        )
        try:
            argv = build_structured_argv(request, schema_file=schema_file)
            stdout, stderr, exit_code = await _run_once(argv, request, prompt)
        finally:
            await _discard_private_file(schema_file)

        if exit_code != 0:
            log.warning(
                "harness.structured_failed",
                harness=HARNESS_NAME,
                exit_code=exit_code,
                stderr_tail=stderr.splitlines()[-STDERR_TAIL_LINES:],
            )
        self.stats = ParseStats()
        return structured_result(
            stdout,
            exit_code,
            model=model,
            cwd=request.cwd or Path.cwd(),
            stats=self.stats,
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
    _signal_process_group(handle.process, sig)


def _signal_process_group(
    process: asyncio.subprocess.Process, sig: signal.Signals
) -> None:
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError):
        pass
