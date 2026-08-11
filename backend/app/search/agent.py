"""Bounded project-branch navigation with evidence-only citations."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from secrets import token_hex
from typing import Any, Protocol, TypeVar, cast, runtime_checkable

from anthropic import APIError, AsyncAnthropic
from anthropic.types import MessageParam, ToolParam
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.models.pricing import PriceTable, TokenCounts
from app.search.tools import CodeSearchService, FileLine, SearchError, file_line_hash

# The stable half of the SDK's auth-resolution `TypeError`, which arrives from a
# private `_validate_headers` hook with no type of its own.
#
# Duplicated from `orchestrator/planner.py` rather than shared: the import-
# linter contract "search/ and metrics/ are isolated verticals" forbids
# `app.search` from importing `app.orchestrator`, and a four-word constant is a
# far smaller price than a shared module that erodes that boundary.
_NO_CREDENTIAL = "Could not resolve authentication method"

SYSTEM_PROMPT = """\
You answer questions about one immutable project branch snapshot by navigating
it with the supplied read-only tools. Every tool is already bound to that same
project and branch; do not infer that you can switch targets. Do not answer from
memory. Search broadly, then read the exact lines that support each claim.

Finish only by calling submit_answer. Every claim needs at least one citation,
and every cited line must have been returned by read_file in this conversation.
Search previews help navigation but are not evidence. Keep claims atomic: if
one sentence needs two locations, cite both. If the evidence is incomplete,
submit only the claims you can support.
"""


class SearchLimitReason(StrEnum):
    TURNS = "turns"
    TOOL_CALLS = "tool_calls"
    BYTES = "bytes"
    TOKENS = "tokens"
    MODEL_ENDED = "model_ended"
    API_ERROR = "api_error"
    REFUSED = "refused"
    TRUNCATED = "truncated"
    NOT_CONFIGURED = "not_configured"
    """No Anthropic credential resolved, so no request was made.

    Unlike every other reason here, nothing was attempted and nothing was
    spent. Kept distinct from `API_ERROR` because a retry cannot help — the
    operator has to supply a credential, and saying "the API failed" sends them
    looking for an outage instead.
    """


class Citation(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class AnswerClaim(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    citations: list[Citation] = Field(min_length=1, max_length=12)


class SubmitAnswerInput(BaseModel):
    claims: list[AnswerClaim] = Field(min_length=1, max_length=30)


class HarnessSearchAction(BaseModel):
    """One backend-owned search action selected by a structured completer."""

    action: str
    pattern: str | None = None
    glob: str | None = None
    case_sensitive: bool | None = None
    literal: bool | None = None
    limit: int | None = None
    language: str | None = None
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    claims: list[AnswerClaim] | None = None


@dataclass(frozen=True, slots=True)
class ValidatedCitation:
    path: str
    line: int
    end_line: int
    content_hash: str


@dataclass(frozen=True, slots=True)
class ValidatedClaim:
    text: str
    citations: tuple[ValidatedCitation, ...]


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    path: str
    line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class SearchUsage:
    model: str
    counts: TokenCounts
    cost_usd: float | None
    price_table_version: int
    requests: int = 0

    def with_request(
        self,
        counts: TokenCounts,
        prices: PriceTable,
        *,
        model: str | None = None,
    ) -> SearchUsage:
        resolved_model = model or self.model
        total = self.counts + counts
        request_cost = prices.cost_usd(resolved_model, counts)
        cost_usd = (
            request_cost
            if self.requests == 0
            else None
            if self.cost_usd is None or request_cost is None
            else self.cost_usd + request_cost
        )
        return SearchUsage(
            model=resolved_model,
            counts=total,
            cost_usd=cost_usd,
            price_table_version=prices.version,
            requests=self.requests + 1,
        )


@dataclass(frozen=True, slots=True)
class SearchAnswer:
    claims: tuple[ValidatedClaim, ...]
    evidence: tuple[EvidenceSpan, ...]
    complete: bool
    limit_reason: SearchLimitReason | None
    message: str
    turns: int
    tool_calls: int
    bytes_read: int
    usage: SearchUsage


@dataclass(frozen=True, slots=True)
class ModelToolCall:
    id: str
    name: str
    input: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelTurn:
    stop_reason: str | None
    assistant_content: Sequence[Mapping[str, Any]]
    tool_calls: tuple[ModelToolCall, ...]
    counts: TokenCounts
    model: str | None = None


class SearchModelClient(Protocol):
    async def complete(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: Sequence[MessageParam],
        tools: Sequence[ToolParam],
    ) -> ModelTurn: ...

    async def close(self) -> None: ...


class SearchModelBackendError(Exception):
    """A configured model backend could not produce one valid search action."""


@dataclass(frozen=True, slots=True)
class HarnessSearchRequest:
    """Harness-neutral structured request assembled by the search vertical.

    Composition maps this value to the harness package's concrete request. The
    search vertical consequently never imports or branches on a harness.
    """

    prompt: str
    schema: Mapping[str, object]
    system: str | None
    model: str | None
    cwd: None = None
    env: Mapping[str, str] = field(default_factory=dict)
    launcher: tuple[str, ...] = ()


RequestT = TypeVar("RequestT", contravariant=True)


@runtime_checkable
class StructuredCompleterLike(Protocol[RequestT]):
    async def complete_structured(self, request: RequestT) -> Any: ...


HARNESS_ACTION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "search_text",
                "search_structural",
                "read_file",
                "list_directory",
                "submit_answer",
            ],
        },
        "pattern": {"type": ["string", "null"]},
        "glob": {"type": ["string", "null"]},
        "case_sensitive": {"type": ["boolean", "null"]},
        "literal": {"type": ["boolean", "null"]},
        "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 500},
        "language": {"type": ["string", "null"]},
        "path": {"type": ["string", "null"]},
        "start_line": {"type": ["integer", "null"], "minimum": 1},
        "end_line": {"type": ["integer", "null"], "minimum": 1},
        "claims": {
            "type": ["array", "null"],
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "citations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "line": {"type": "integer", "minimum": 1},
                                "end_line": {"type": "integer", "minimum": 1},
                            },
                            "required": ["path", "line", "end_line"],
                            "additionalProperties": False,
                        },
                        "minItems": 1,
                        "maxItems": 12,
                    },
                },
                "required": ["text", "citations"],
                "additionalProperties": False,
            },
            "minItems": 1,
            "maxItems": 30,
        },
    },
    "required": [
        "action",
        "pattern",
        "glob",
        "case_sensitive",
        "literal",
        "limit",
        "language",
        "path",
        "start_line",
        "end_line",
        "claims",
    ],
    "additionalProperties": False,
}


class HarnessSearchClient[ConcreteRequestT]:
    """Stateless adapter from the chat loop to one structured CLI process."""

    def __init__(
        self,
        *,
        completer: StructuredCompleterLike[ConcreteRequestT],
        request_factory: Callable[[HarnessSearchRequest], ConcreteRequestT],
        model: str | None,
        launcher: tuple[str, ...],
        max_transcript_bytes: int,
        backend_errors: tuple[type[Exception], ...] = (),
    ) -> None:
        self._completer = completer
        self._request_factory = request_factory
        self._model = model
        self._launcher = launcher
        self._max_transcript_bytes = max_transcript_bytes
        self._backend_errors = backend_errors

    async def complete(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: Sequence[MessageParam],
        tools: Sequence[ToolParam],
    ) -> ModelTurn:
        del model, max_tokens
        transcript = json.dumps(
            {"messages": messages, "tools": tools},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        prompt = (
            "Select exactly one next action for the repository-search loop. "
            "The application executes it and will provide its result on the "
            "next turn. Populate fields unrelated to the selected action with "
            "null; for submit_answer, populate claims and use null elsewhere.\n\n"
            f"Transcript and available tools:\n{transcript}"
        )
        if len(prompt.encode("utf-8")) > self._max_transcript_bytes:
            raise SearchModelBackendError(
                "the structured-search transcript exceeded its byte ceiling"
            )
        request = self._request_factory(
            HarnessSearchRequest(
                prompt=prompt,
                schema=HARNESS_ACTION_SCHEMA,
                system=system,
                # ``None`` is meaningful: let the selected CLI use its own
                # configured default. The protocol's `model` argument belongs
                # to the API path and must never leak an Anthropic model name
                # into Codex or another harness.
                model=self._model,
                cwd=None,
                launcher=self._launcher,
            )
        )
        try:
            result = await self._completer.complete_structured(request)
        except self._backend_errors as error:
            raise SearchModelBackendError(
                f"the structured search backend failed: {type(error).__name__}"
            ) from error

        try:
            action = HarnessSearchAction.model_validate(result.data)
            name, values = _harness_action_call(action)
            counts = _structured_token_counts(result.usage)
            actual_model = str(result.model)
        except (AttributeError, TypeError, ValueError, ValidationError) as error:
            raise SearchModelBackendError(
                "the structured search backend returned an invalid action"
            ) from error

        call = ModelToolCall(
            id=f"search_{token_hex(8)}",
            name=name,
            input=values,
        )
        content: Mapping[str, Any] = {
            "type": "tool_use",
            "id": call.id,
            "name": call.name,
            "input": dict(call.input),
        }
        return ModelTurn(
            stop_reason="tool_use",
            assistant_content=(content,),
            tool_calls=(call,),
            counts=counts,
            model=actual_model,
        )

    async def close(self) -> None:
        # Structured completers launch one process per request and own no
        # persistent session for this client to close.
        return None


def create_harness_search_client[ConcreteRequestT](
    *,
    completer: StructuredCompleterLike[ConcreteRequestT],
    request_factory: Callable[[HarnessSearchRequest], ConcreteRequestT],
    model: str | None,
    launcher: tuple[str, ...],
    max_transcript_bytes: int,
    backend_errors: tuple[type[Exception], ...] = (),
) -> HarnessSearchClient[ConcreteRequestT]:
    """Create the subscription-backed client after a capability-only check."""
    if not isinstance(completer, StructuredCompleterLike):
        raise ValueError("search adapter does not support structured output")
    return HarnessSearchClient(
        completer=completer,
        request_factory=request_factory,
        model=model,
        launcher=launcher,
        max_transcript_bytes=max_transcript_bytes,
        backend_errors=backend_errors,
    )


class AnthropicSearchClient:
    """Thin SDK boundary; the bounded loop remains independently testable."""

    def __init__(self, client: AsyncAnthropic | None = None) -> None:
        self._client = client or AsyncAnthropic()

    async def complete(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: Sequence[MessageParam],
        tools: Sequence[ToolParam],
    ) -> ModelTurn:
        response = await self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=list(messages),
            tools=list(tools),
        )
        content = tuple(
            cast(Mapping[str, Any], block.model_dump(exclude_none=True))
            for block in response.content
        )
        calls = tuple(
            ModelToolCall(id=block.id, name=block.name, input=block.input)
            for block in response.content
            if block.type == "tool_use"
        )
        return ModelTurn(
            stop_reason=response.stop_reason,
            assistant_content=content,
            tool_calls=calls,
            counts=_token_counts(response.usage),
            model=response.model,
        )

    async def close(self) -> None:
        await self._client.close()


def _tool(
    name: str, description: str, properties: Mapping[str, Any], required: list[str]
) -> ToolParam:
    return cast(
        ToolParam,
        {
            "name": name,
            "description": description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": dict(properties),
                "required": required,
                "additionalProperties": False,
            },
        },
    )


TOOLS: tuple[ToolParam, ...] = (
    _tool(
        "search_text",
        "Search repository text. Results are navigation hints, not evidence.",
        {
            "pattern": {"type": "string"},
            "glob": {"type": ["string", "null"]},
            "case_sensitive": {"type": "boolean"},
            "literal": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        ["pattern", "case_sensitive", "literal", "limit"],
    ),
    _tool(
        "search_structural",
        "Search syntax with an ast-grep pattern. Results are navigation hints.",
        {
            "pattern": {"type": "string"},
            "language": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        ["pattern", "language", "limit"],
    ),
    _tool(
        "read_file",
        "Read a bounded line range. Only lines returned here may be cited.",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        ["path", "start_line", "end_line"],
    ),
    _tool(
        "list_directory",
        "List one repository directory without recursion.",
        {
            "path": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        },
        ["path", "limit"],
    ),
    _tool(
        "submit_answer",
        "Submit evidence-backed claims. Call only after reading every cited line.",
        {
            "claims": {
                "type": "array",
                "minItems": 1,
                "maxItems": 30,
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {"type": "string"},
                        "citations": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 12,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "line": {"type": "integer", "minimum": 1},
                                    "end_line": {"type": "integer", "minimum": 1},
                                },
                                "required": ["path", "line", "end_line"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["text", "citations"],
                    "additionalProperties": False,
                },
            }
        },
        ["claims"],
    ),
)


class SearchAgent:
    def __init__(
        self,
        *,
        client: SearchModelClient,
        tools: CodeSearchService,
        settings: Settings,
        prices: PriceTable,
    ) -> None:
        self._client = client
        self._tools = tools
        self._settings = settings
        self._prices = prices

    async def close(self) -> None:
        await self._client.close()

    async def answer(self, project_id: str, branch: str, question: str) -> SearchAnswer:
        if not question.strip():
            raise ValueError("search question must not be empty")
        await self._tools.validate_target(project_id, branch)
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        evidence: dict[str, dict[int, str]] = {}
        usage = SearchUsage(
            model=self._settings.search_model,
            counts=TokenCounts(),
            cost_usd=None,
            price_table_version=self._prices.version,
        )
        turns = tool_calls = bytes_read = 0

        while turns < self._settings.search_max_turns:
            turns += 1
            try:
                turn = await self._client.complete(
                    model=self._settings.search_model,
                    max_tokens=self._settings.search_max_output_tokens,
                    system=SYSTEM_PROMPT,
                    messages=messages,
                    tools=TOOLS,
                )
            except TypeError as type_error:
                # The SDK reports "no credential" as a bare `TypeError` from a
                # private `_validate_headers` hook, so the message is the only
                # thing that distinguishes it from a real bug in this call —
                # which must keep propagating. Same shape as the planner's
                # handling of the same SDK behavior.
                if _NO_CREDENTIAL not in str(type_error):
                    raise
                return self._partial(
                    SearchLimitReason.NOT_CONFIGURED,
                    "the Anthropic API runtime selected for Code Search has no "
                    "credential. Select a subscription-backed harness in "
                    "Settings, or set ANTHROPIC_API_KEY in the environment "
                    "that starts the server and restart it.",
                    evidence,
                    turns,
                    tool_calls,
                    bytes_read,
                    usage,
                )
            except (APIError, SearchModelBackendError) as api_error:
                return self._partial(
                    SearchLimitReason.API_ERROR,
                    f"the search model backend failed: {type(api_error).__name__}",
                    evidence,
                    turns,
                    tool_calls,
                    bytes_read,
                    usage,
                )
            usage = usage.with_request(
                turn.counts,
                self._prices,
                model=turn.model,
            )
            if usage.counts.total > self._settings.search_max_tokens:
                return self._partial(
                    SearchLimitReason.TOKENS,
                    "search stopped at the cumulative model-token ceiling",
                    evidence,
                    turns,
                    tool_calls,
                    bytes_read,
                    usage,
                )

            messages.append(
                cast(
                    MessageParam,
                    {"role": "assistant", "content": list(turn.assistant_content)},
                )
            )
            if turn.stop_reason != "tool_use":
                reason = (
                    SearchLimitReason.REFUSED
                    if turn.stop_reason == "refusal"
                    else SearchLimitReason.TRUNCATED
                    if turn.stop_reason
                    in {"max_tokens", "model_context_window_exceeded"}
                    else SearchLimitReason.MODEL_ENDED
                )
                return self._partial(
                    reason,
                    "search model ended without a validated answer "
                    f"({turn.stop_reason})",
                    evidence,
                    turns,
                    tool_calls,
                    bytes_read,
                    usage,
                )
            if not turn.tool_calls:
                return self._partial(
                    SearchLimitReason.MODEL_ENDED,
                    "search model requested tools without a tool call",
                    evidence,
                    turns,
                    tool_calls,
                    bytes_read,
                    usage,
                )

            results: list[dict[str, Any]] = []
            for call in turn.tool_calls:
                if tool_calls >= self._settings.search_max_tool_calls:
                    return self._partial(
                        SearchLimitReason.TOOL_CALLS,
                        "search stopped at the tool-call ceiling",
                        evidence,
                        turns,
                        tool_calls,
                        bytes_read,
                        usage,
                    )
                tool_calls += 1
                if call.name == "submit_answer":
                    answer, validation_error = _validate_answer(call.input, evidence)
                    if answer is not None:
                        return SearchAnswer(
                            claims=answer,
                            evidence=_evidence_spans(evidence),
                            complete=True,
                            limit_reason=None,
                            message="answer validated against read-file evidence",
                            turns=turns,
                            tool_calls=tool_calls,
                            bytes_read=bytes_read,
                            usage=usage,
                        )
                    payload = {"error": validation_error}
                    is_error = True
                    read_lines: dict[str, dict[int, str]] = {}
                else:
                    payload, is_error, read_lines = await self._execute(
                        project_id,
                        branch,
                        call,
                    )
                encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                size = len(encoded.encode("utf-8"))
                if bytes_read + size > self._settings.search_max_bytes:
                    return self._partial(
                        SearchLimitReason.BYTES,
                        "search stopped at the tool-result byte ceiling",
                        evidence,
                        turns,
                        tool_calls,
                        bytes_read,
                        usage,
                    )
                bytes_read += size
                for path, lines in read_lines.items():
                    evidence.setdefault(path, {}).update(lines)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": encoded,
                        "is_error": is_error,
                    }
                )
            messages.append(cast(MessageParam, {"role": "user", "content": results}))

        return self._partial(
            SearchLimitReason.TURNS,
            "search stopped at the model-turn ceiling",
            evidence,
            turns,
            tool_calls,
            bytes_read,
            usage,
        )

    async def _execute(
        self,
        project_id: str,
        branch: str,
        call: ModelToolCall,
    ) -> tuple[dict[str, Any], bool, dict[str, dict[int, str]]]:
        values = dict(call.input)
        read: dict[str, dict[int, str]] = {}
        result: Any
        try:
            if call.name == "search_text":
                result = await self._tools.search_text(project_id, branch, **values)
            elif call.name == "search_structural":
                result = await self._tools.search_structural(
                    project_id, branch, **values
                )
            elif call.name == "read_file":
                result = await self._tools.read_file(project_id, branch, **values)
                read[result.path] = {line.line: line.text for line in result.lines}
            elif call.name == "list_directory":
                result = await self._tools.list_directory(project_id, branch, **values)
            else:
                return {"error": f"unknown tool: {call.name}"}, True, read
            return asdict(result), False, read
        except (SearchError, ValidationError, TypeError, ValueError) as error:
            return {"error": str(error)}, True, read

    def _partial(
        self,
        reason: SearchLimitReason,
        message: str,
        evidence: Mapping[str, Mapping[int, str]],
        turns: int,
        tool_calls: int,
        bytes_read: int,
        usage: SearchUsage,
    ) -> SearchAnswer:
        return SearchAnswer(
            claims=(),
            evidence=_evidence_spans(evidence),
            complete=False,
            limit_reason=reason,
            message=message,
            turns=turns,
            tool_calls=tool_calls,
            bytes_read=bytes_read,
            usage=usage,
        )


def _validate_answer(
    raw: Mapping[str, Any], evidence: Mapping[str, Mapping[int, str]]
) -> tuple[tuple[ValidatedClaim, ...] | None, str | None]:
    try:
        answer = SubmitAnswerInput.model_validate(raw)
    except ValidationError as error:
        return None, f"answer schema is invalid: {error.errors(include_url=False)}"
    claims: list[ValidatedClaim] = []
    for claim in answer.claims:
        citations: list[ValidatedCitation] = []
        for citation in claim.citations:
            if citation.end_line < citation.line:
                return None, f"citation range is reversed: {citation.path}"
            available = evidence.get(citation.path, {})
            required = set(range(citation.line, citation.end_line + 1))
            if not required.issubset(available.keys()):
                return None, (
                    "citation was not read with read_file: "
                    f"{citation.path}:{citation.line}-{citation.end_line}"
                )
            cited_lines = tuple(
                FileLine(line=line, text=available[line]) for line in sorted(required)
            )
            citations.append(
                ValidatedCitation(
                    path=citation.path,
                    line=citation.line,
                    end_line=citation.end_line,
                    content_hash=file_line_hash(cited_lines),
                )
            )
        claims.append(ValidatedClaim(text=claim.text, citations=tuple(citations)))
    return tuple(claims), None


def _evidence_spans(
    evidence: Mapping[str, Mapping[int, str]],
) -> tuple[EvidenceSpan, ...]:
    spans: list[EvidenceSpan] = []
    for path in sorted(evidence):
        lines = sorted(evidence[path])
        if not lines:
            continue
        start = previous = lines[0]
        for line in lines[1:]:
            if line == previous + 1:
                previous = line
                continue
            spans.append(EvidenceSpan(path=path, line=start, end_line=previous))
            start = previous = line
        spans.append(EvidenceSpan(path=path, line=start, end_line=previous))
    return tuple(spans)


def _token_counts(usage: Any) -> TokenCounts:
    creation = getattr(usage, "cache_creation", None)
    write_total = getattr(usage, "cache_creation_input_tokens", None) or 0
    write_5m = getattr(creation, "ephemeral_5m_input_tokens", 0) if creation else 0
    write_1h = getattr(creation, "ephemeral_1h_input_tokens", 0) if creation else 0
    if write_5m + write_1h != write_total:
        write_5m = write_1h = 0
    return TokenCounts(
        input_tokens=int(usage.input_tokens),
        output_tokens=int(usage.output_tokens),
        cache_read_tokens=int(getattr(usage, "cache_read_input_tokens", None) or 0),
        cache_write_tokens=int(write_total),
        cache_write_5m_tokens=int(write_5m),
        cache_write_1h_tokens=int(write_1h),
    )


def _structured_token_counts(usage: Any) -> TokenCounts:
    if usage is None:
        return TokenCounts()
    return TokenCounts(
        input_tokens=int(usage.input_tokens),
        output_tokens=int(usage.output_tokens),
        cache_read_tokens=int(usage.cache_read_tokens),
        cache_write_tokens=int(usage.cache_write_tokens),
        cache_write_5m_tokens=int(getattr(usage, "cache_write_5m_tokens", 0)),
        cache_write_1h_tokens=int(getattr(usage, "cache_write_1h_tokens", 0)),
    )


def _harness_action_call(
    action: HarnessSearchAction,
) -> tuple[str, Mapping[str, Any]]:
    if action.action == "search_text":
        if action.pattern is None:
            raise ValueError("search_text requires pattern")
        return action.action, {
            "pattern": action.pattern,
            "glob": action.glob,
            "case_sensitive": action.case_sensitive
            if action.case_sensitive is not None
            else False,
            "literal": action.literal if action.literal is not None else True,
            "limit": action.limit or 50,
        }
    if action.action == "search_structural":
        if action.pattern is None or action.language is None:
            raise ValueError("search_structural requires pattern and language")
        return action.action, {
            "pattern": action.pattern,
            "language": action.language,
            "limit": action.limit or 50,
        }
    if action.action == "read_file":
        if action.path is None or action.start_line is None or action.end_line is None:
            raise ValueError("read_file requires path, start_line, and end_line")
        return action.action, {
            "path": action.path,
            "start_line": action.start_line,
            "end_line": action.end_line,
        }
    if action.action == "list_directory":
        return action.action, {
            "path": action.path or ".",
            "limit": action.limit or 100,
        }
    if action.action == "submit_answer":
        if action.claims is None:
            raise ValueError("submit_answer requires claims")
        return action.action, {
            "claims": [claim.model_dump() for claim in action.claims]
        }
    raise ValueError(f"unknown structured search action: {action.action}")


def create_search_agent(
    *,
    tools: CodeSearchService,
    settings: Settings,
    prices: PriceTable,
) -> SearchAgent:
    return SearchAgent(
        client=AnthropicSearchClient(),
        tools=tools,
        settings=settings,
        prices=prices,
    )


__all__ = [
    "HARNESS_ACTION_SCHEMA",
    "AnswerClaim",
    "Citation",
    "EvidenceSpan",
    "HarnessSearchClient",
    "HarnessSearchRequest",
    "ModelToolCall",
    "ModelTurn",
    "SearchAgent",
    "SearchAnswer",
    "SearchLimitReason",
    "SearchModelBackendError",
    "SearchUsage",
    "ValidatedCitation",
    "ValidatedClaim",
    "create_harness_search_client",
    "create_search_agent",
]
