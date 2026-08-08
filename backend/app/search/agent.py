"""Bounded model-driven repository navigation with evidence-only citations."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Protocol, cast

from anthropic import APIError, AsyncAnthropic
from anthropic.types import MessageParam, ToolParam
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings
from app.models.ids import SessionId
from app.models.pricing import PriceTable, TokenCounts
from app.search.symbols import SymbolIndexService
from app.search.tools import CodeSearchService, SearchError

SYSTEM_PROMPT = """\
You answer questions about one repository by navigating it with the supplied
read-only tools. Do not answer from memory. Search broadly, then read the exact
lines that support each claim.

Finish only by calling submit_answer. Every claim needs at least one citation,
and every cited line must have been returned by read_file in this conversation.
Search previews and symbol matches help navigation but are not evidence. Keep
claims atomic: if one sentence needs two locations, cite both. If the evidence
is incomplete, submit only the claims you can support.
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


class Citation(BaseModel):
    path: str = Field(min_length=1, max_length=1_000)
    line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class AnswerClaim(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    citations: list[Citation] = Field(min_length=1, max_length=12)


class SubmitAnswerInput(BaseModel):
    claims: list[AnswerClaim] = Field(min_length=1, max_length=30)


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

    def with_request(self, counts: TokenCounts, prices: PriceTable) -> SearchUsage:
        total = self.counts + counts
        return SearchUsage(
            model=self.model,
            counts=total,
            cost_usd=prices.cost_usd(self.model, total),
            price_table_version=prices.version,
            requests=self.requests + 1,
        )


@dataclass(frozen=True, slots=True)
class SearchAnswer:
    claims: tuple[AnswerClaim, ...]
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
        "find_symbol",
        "Find exact symbol definitions from the incremental Tree-sitter index.",
        {
            "name": {"type": "string"},
            "kind": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        ["name", "limit"],
    ),
    _tool(
        "find_references",
        "Find exact call references from the incremental Tree-sitter index.",
        {
            "name": {"type": "string"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        ["name", "limit"],
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
        symbols: SymbolIndexService,
        settings: Settings,
        prices: PriceTable,
    ) -> None:
        self._client = client
        self._tools = tools
        self._symbols = symbols
        self._settings = settings
        self._prices = prices

    async def close(self) -> None:
        await self._client.close()

    async def answer(self, session_id: SessionId, question: str) -> SearchAnswer:
        if not question.strip():
            raise ValueError("search question must not be empty")
        await self._tools.validate_target(session_id)
        messages: list[MessageParam] = [{"role": "user", "content": question}]
        evidence: dict[str, set[int]] = {}
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
            except APIError as api_error:
                return self._partial(
                    SearchLimitReason.API_ERROR,
                    f"the search model API failed: {type(api_error).__name__}",
                    evidence,
                    turns,
                    tool_calls,
                    bytes_read,
                    usage,
                )
            usage = usage.with_request(turn.counts, self._prices)
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
                            claims=tuple(answer.claims),
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
                    read_lines: dict[str, set[int]] = {}
                else:
                    payload, is_error, read_lines = await self._execute(
                        session_id, call
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
                    evidence.setdefault(path, set()).update(lines)
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
        self, session_id: SessionId, call: ModelToolCall
    ) -> tuple[dict[str, Any], bool, dict[str, set[int]]]:
        values = dict(call.input)
        read: dict[str, set[int]] = {}
        result: Any
        try:
            if call.name == "search_text":
                result = await self._tools.search_text(session_id, **values)
            elif call.name == "search_structural":
                result = await self._tools.search_structural(session_id, **values)
            elif call.name == "find_symbol":
                result = await self._symbols.find_symbol(session_id, **values)
            elif call.name == "find_references":
                result = await self._symbols.find_references(session_id, **values)
            elif call.name == "read_file":
                result = await self._tools.read_file(session_id, **values)
                read[result.path] = {line.line for line in result.lines}
            elif call.name == "list_directory":
                result = await self._tools.list_directory(session_id, **values)
            else:
                return {"error": f"unknown tool: {call.name}"}, True, read
            return asdict(result), False, read
        except (SearchError, ValidationError, TypeError, ValueError) as error:
            return {"error": str(error)}, True, read

    def _partial(
        self,
        reason: SearchLimitReason,
        message: str,
        evidence: Mapping[str, set[int]],
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
    raw: Mapping[str, Any], evidence: Mapping[str, set[int]]
) -> tuple[SubmitAnswerInput | None, str | None]:
    try:
        answer = SubmitAnswerInput.model_validate(raw)
    except ValidationError as error:
        return None, f"answer schema is invalid: {error.errors(include_url=False)}"
    for claim in answer.claims:
        for citation in claim.citations:
            if citation.end_line < citation.line:
                return None, f"citation range is reversed: {citation.path}"
            available = evidence.get(citation.path, set())
            required = set(range(citation.line, citation.end_line + 1))
            if not required.issubset(available):
                return None, (
                    "citation was not read with read_file: "
                    f"{citation.path}:{citation.line}-{citation.end_line}"
                )
    return answer, None


def _evidence_spans(evidence: Mapping[str, set[int]]) -> tuple[EvidenceSpan, ...]:
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


def create_search_agent(
    *,
    tools: CodeSearchService,
    symbols: SymbolIndexService,
    settings: Settings,
    prices: PriceTable,
) -> SearchAgent:
    return SearchAgent(
        client=AnthropicSearchClient(),
        tools=tools,
        symbols=symbols,
        settings=settings,
        prices=prices,
    )


__all__ = [
    "AnswerClaim",
    "Citation",
    "EvidenceSpan",
    "ModelToolCall",
    "ModelTurn",
    "SearchAgent",
    "SearchAnswer",
    "SearchLimitReason",
    "SearchUsage",
    "create_search_agent",
]
