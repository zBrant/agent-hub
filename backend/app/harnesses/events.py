"""``AgentEvent`` — the one type every harness normalizes to.

`docs/architecture.md` §2: outside ``app/harnesses/`` nobody knows which CLI is
running. ``harness`` and ``model`` are *data* on these events, never behavioural
conditionals (invariant 1). The same serialization feeds three destinations —
a line in ``events.ndjson``, a WebSocket frame, and a replay payload — so every
variant must survive ``model_dump(mode="json")`` → ``model_validate`` unchanged
(invariant 4).

The union starts from `design.md` §3 and adds two variants that the real Claude
Code stream forced (see ``claude_code.py`` and the A3 capture notes in
``tests/fixtures/claude-code/NOTES.md``):

``TurnStarted`` / ``TurnFinished``
    Claude Code emits ``system/init`` **and** ``result`` once per *turn*, not
    once per run: ``multi_turn.ndjson`` has ``init`` at lines 1 and 13 and
    ``result`` at lines 12 and 23, all with one ``session_id``. Mapping those
    onto ``RunStarted``/``RunFinished`` would report one run per turn — every
    count in the dashboard multiplied by the number of turns. A run is the
    process; a turn is one request/response cycle inside it.

    This is not Claude-specific: Codex ``exec`` and OpenCode both have a
    request/response cycle inside one process. The concept generalizes, which is
    why it lives in the event and not in a harness conditional.

Two shapes carry deliberate deviations from `design.md` §3, both documented on
the classes themselves: ``AssistantText``/``ThinkingDelta`` carry a whole content
block rather than a delta, and ``Usage`` splits the cache-write tier.

Adding a field to an existing variant is always **optional with a default** —
old NDJSON has to keep loading (`docs/conventions.md` §2).
"""

import base64
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    model_validator,
)

from app.models.ids import RunId


def _decode_base64(value: object) -> object:
    return base64.b64decode(value, validate=True) if isinstance(value, str) else value


# Raw bytes in Python, base64 on the wire. Pydantic's own ``Base64Bytes`` also
# *decodes on input*, which would make ``RawChunk(data=pty_bytes)`` fail on any
# chunk that is not itself valid base64 — the opposite of what Channel B needs.
BinaryPayload = Annotated[
    bytes,
    BeforeValidator(_decode_base64),
    PlainSerializer(
        lambda value: base64.b64encode(value).decode("ascii"),
        return_type=str,
        when_used="json",
    ),
]

# What a run or a turn ended as. Deliberately harness-neutral: every harness can
# succeed, fail, be interrupted, or hit a spend cap. `docs/architecture.md` §9 —
# an agent failing is data, not an exception.
type RunStatus = Literal["success", "failed", "interrupted", "budget_exceeded"]


class _Event(BaseModel):
    """Fields every event carries.

    Frozen because an event is a fact: once it is in ``events.ndjson`` nothing
    downstream may edit it. ``extra="forbid"`` so a typo in a replayed line
    fails loudly instead of silently dropping a token count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: RunId
    # int milliseconds UTC (docs/conventions.md §2). When the harness stamps the
    # line itself we use its stamp; otherwise it is our ingest time.
    ts: int


class RunStarted(_Event):
    """The process is up. Exactly one per run, whatever the harness repeats.

    ``session_id`` is the *harness's* own identifier and ``run_id`` is ours —
    both are kept because the harness id is what you need to resume or to
    correlate with the CLI's own logs, and ours is what the database keys on.
    """

    type: Literal["run_started"] = "run_started"
    harness: str
    model: str
    cwd: Path
    # None while parsing a recorded stream: a fixture has no process.
    pid: int | None = None
    session_id: str | None = None
    harness_version: str | None = None


class TurnStarted(_Event):
    """One request/response cycle begins. ``turn`` is 1-based and per run."""

    type: Literal["turn_started"] = "turn_started"
    turn: int = Field(ge=1)
    model: str
    session_id: str | None = None


class AssistantText(_Event):
    """Assistant-visible text.

    `design.md` §3 calls this ``AssistantText(delta)``. It is **not** a delta in
    Claude Code 2.1.222 without ``--include-partial-messages``: text arrives as
    a complete content block on an ``assistant`` line. The field is therefore
    ``text``, and the contract for consumers is *append* — which is correct for
    both whole blocks and the true deltas Phase 1 will emit from
    ``stream_event/content_block_delta``. No consumer needs to know which it got.
    """

    type: Literal["assistant_text"] = "assistant_text"
    text: str


class ThinkingDelta(_Event):
    """Extended-thinking text. Same whole-block caveat as :class:`AssistantText`.

    The class name is `design.md` §3's; the payload field is ``text`` for the
    reason above. The provider's ``signature`` blob on a thinking block is
    dropped — it is an opaque re-submission token for the API, has no meaning to
    a dashboard, and is large.
    """

    type: Literal["thinking_delta"] = "thinking_delta"
    text: str


class ToolCall(_Event):
    """The agent invoked a tool. ``call_id`` correlates with :class:`ToolResult`."""

    type: Literal["tool_call"] = "tool_call"
    call_id: str
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)


class ToolResult(_Event):
    """The outcome of a :class:`ToolCall`.

    ``denied`` separates *the sandbox or the permission layer refused this* from
    *the tool ran and failed*. Both arrive with ``ok=False`` and they mean very
    different things to the operator: a denial is a policy problem, an error is
    the agent's problem. Claude Code marks the first with
    ``tool_result_meta[].non_execution_kind == "user-rejected"``.
    """

    type: Literal["tool_result"] = "tool_result"
    call_id: str
    ok: bool
    preview: str = ""
    denied: bool = False


class Usage(_Event):
    """Token consumption. The four fields of invariant 3, plus the tier split.

    ``input_tokens + output_tokens + cache_read_tokens + cache_write_tokens`` is
    the contract; summing only ``input_tokens`` makes a long session look ~100x
    cheaper than it was.

    ``cache_write_5m_tokens`` / ``cache_write_1h_tokens`` are a **breakdown of**
    ``cache_write_tokens``, not additions to it, and the validator below enforces
    that. They exist because `design.md` §4 prices the two TTLs differently
    (~1.25x vs ~2.0x input), so collapsing them is up to a 1.6x cost error — and
    every A3 capture is 100% ``ephemeral_1h``, i.e. the expensive tier is the
    default in Claude Code 2.1.222, not the exception. Only pricing reads them;
    the four-field contract is unchanged. Both default to 0 for harnesses that
    do not report a split.

    ``model`` is the raw string the harness reported, never normalized here —
    Claude Code spells the same model two ways (``claude-haiku-4-5`` on
    ``system/init``, ``claude-haiku-4-5-20251001`` on ``assistant`` lines) and
    deciding which one prices correctly belongs to the pricing table, not to a
    parser that would have to throw one of them away.
    """

    type: Literal["usage"] = "usage"
    model: str
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cache_read_tokens: int = Field(default=0, ge=0)
    cache_write_tokens: int = Field(default=0, ge=0)
    cache_write_5m_tokens: int = Field(default=0, ge=0)
    cache_write_1h_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _tier_split_must_reconcile(self) -> Self:
        split = self.cache_write_5m_tokens + self.cache_write_1h_tokens
        if split and split != self.cache_write_tokens:
            raise ValueError(
                "cache write tier split does not reconcile: "
                f"5m={self.cache_write_5m_tokens} + 1h={self.cache_write_1h_tokens} "
                f"!= cache_write_tokens={self.cache_write_tokens}"
            )
        return self

    @property
    def total_tokens(self) -> int:
        """All four billable fields. Never sum a subset (invariant 3)."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


class Permission(_Event):
    """A human gate: the agent is asking to do something and is blocked on it.

    **Channel A never produces this in ``-p`` mode.** Verified against Claude
    Code 2.1.222: with no ``--permission-mode`` the CLI does not emit a request
    at all — it refuses silently, the refusal surfaces as an ordinary
    ``is_error`` tool result, and the run still reports success with exit 0. The
    only trace is ``result.permission_denials[]``, which lands on
    :class:`TurnFinished` *after the fact*. There is no ``request_id`` to answer.

    The variant stays declared because `design.md` §3 specifies it and other
    harnesses (or an MCP permission-prompt tool) may yet produce it. Nothing in
    Phase 0 emits it; do not build a UI that waits for one.
    """

    type: Literal["permission"] = "permission"
    request_id: str
    description: str


class PermissionDenial(BaseModel):
    """One tool call the permission layer refused. See :class:`TurnFinished`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool: str
    call_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)


class TurnFinished(_Event):
    """One request/response cycle ended.

    **Read ``permission_denials`` before you mark anything done.** A run whose
    every write was refused is reported by Claude Code 2.1.222 as
    ``subtype: "success"``, ``is_error: false``, exit code 0 — indistinguishable
    from a run that did the work. ``status`` will say ``"success"`` because that
    is what the harness said; the non-empty ``permission_denials`` tuple is the
    only signal in the data, and :attr:`blocked_by_permission` is how the
    scheduler must consume it. A node that transitions to *done* while
    ``blocked_by_permission`` is true has reported a lie to the user and merged
    an empty diff.

    ``errors`` carries the harness's own failure strings. They are generalized
    rather than dropped because a failed turn with no explanation is
    unactionable for the operator.

    The harness's self-reported cost is deliberately **not** here: Claude Code's
    ``total_cost_usd`` accumulates across turns (summing it double-counts) and
    includes side-channel model calls no event can see. Cost is computed at
    ingest from :class:`Usage` against the pricing table (invariant 3), and is
    labelled *estimated equivalent* under a subscription (invariant 7).
    """

    type: Literal["turn_finished"] = "turn_finished"
    turn: int = Field(ge=1)
    status: RunStatus
    summary: str | None = None
    permission_denials: tuple[PermissionDenial, ...] = ()
    errors: tuple[str, ...] = ()
    session_id: str | None = None
    duration_ms: int | None = None

    @property
    def blocked_by_permission(self) -> bool:
        """True when the agent was refused, whatever ``status`` claims."""
        return bool(self.permission_denials)


class RunFinished(_Event):
    """The process exited. Exactly one per run, and it is the last event.

    ``exit_code`` has **no source in any harness's JSON** — Claude Code's
    ``result`` line carries a status but not the code. The adapter synthesizes
    this event from ``Process.wait()``, so a parser working on a recorded stream
    (replay, fixtures) legitimately produces none.
    """

    type: Literal["run_finished"] = "run_finished"
    status: RunStatus
    exit_code: int | None = None
    summary: str | None = None


class RawChunk(_Event):
    """Channel B only: raw PTY bytes for xterm.js.

    Never derive state from this (`docs/architecture.md` §5) — it is pixels,
    including ANSI cursor addressing and alternate-screen switches. Base64 on
    the wire because PTY output is not valid UTF-8 in general.
    """

    type: Literal["raw_chunk"] = "raw_chunk"
    data: BinaryPayload


AgentEvent = Annotated[
    RunStarted
    | TurnStarted
    | AssistantText
    | ThinkingDelta
    | ToolCall
    | ToolResult
    | Usage
    | Permission
    | TurnFinished
    | RunFinished
    | RawChunk,
    Field(discriminator="type"),
]

# The one way to get an AgentEvent back out of a line of NDJSON or a WebSocket
# frame. Consumers must not re-implement the dispatch on `type`.
agent_event_adapter: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)
