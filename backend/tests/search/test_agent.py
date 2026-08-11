"""E4 bounded agentic search with an evidence ledger."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from anthropic.types import MessageParam, ToolParam

from app.config import Settings
from app.models.pricing import TokenCounts, load_price_table
from app.search.agent import (
    HARNESS_ACTION_SCHEMA,
    HarnessSearchClient,
    HarnessSearchRequest,
    ModelToolCall,
    ModelTurn,
    SearchAgent,
    SearchLimitReason,
    SearchModelBackendError,
    SearchModelClient,
    create_harness_search_client,
)
from app.search.tools import (
    CodeSearchService,
    DirectoryListResult,
    FileLine,
    FileReadResult,
    SearchTargetNotFound,
    TextSearchResult,
    file_line_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


class RecordedModel:
    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self.turns = list(turns)
        self.messages: list[Sequence[MessageParam]] = []
        self.tool_names: list[tuple[str, ...]] = []
        self.system_prompts: list[str] = []
        self.closed = False

    async def complete(
        self,
        *,
        model: str,
        max_tokens: int,
        system: str,
        messages: Sequence[MessageParam],
        tools: Sequence[ToolParam],
    ) -> ModelTurn:
        self.messages.append(deepcopy(messages))
        self.tool_names.append(tuple(tool["name"] for tool in tools))
        self.system_prompts.append(system)
        return self.turns.pop(0)

    async def close(self) -> None:
        self.closed = True


def tool_turn(
    *calls: tuple[str, dict[str, object]], counts: TokenCounts | None = None
) -> ModelTurn:
    tool_calls = tuple(
        ModelToolCall(id=f"tool_{index}", name=name, input=values)
        for index, (name, values) in enumerate(calls)
    )
    return ModelTurn(
        stop_reason="tool_use",
        assistant_content=tuple(
            {
                "type": "tool_use",
                "id": call.id,
                "name": call.name,
                "input": dict(call.input),
            }
            for call in tool_calls
        ),
        tool_calls=tool_calls,
        counts=counts or TokenCounts(input_tokens=10, output_tokens=5),
    )


def end_turn() -> ModelTurn:
    return ModelTurn(
        stop_reason="end_turn",
        assistant_content=({"type": "text", "text": "unsupported prose"},),
        tool_calls=(),
        counts=TokenCounts(input_tokens=10, output_tokens=5),
    )


class ProjectTools:
    """Small project/branch tool seam; filesystem behavior belongs to tools.py."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.targets: list[tuple[str, str, str]] = []
        self.target_error: SearchTargetNotFound | None = None

    async def validate_target(self, project_id: str, branch: str) -> None:
        self.targets.append(("validate_target", project_id, branch))
        if self.target_error is not None:
            raise self.target_error

    async def search_text(
        self, project_id: str, branch: str, *_args: object, **_kwargs: object
    ) -> TextSearchResult:
        self.targets.append(("search_text", project_id, branch))
        return TextSearchResult(matches=(), truncated=False)

    async def search_structural(
        self, project_id: str, branch: str, *_args: object, **_kwargs: object
    ) -> TextSearchResult:
        self.targets.append(("search_structural", project_id, branch))
        return TextSearchResult(matches=(), truncated=False)

    async def read_file(
        self,
        project_id: str,
        branch: str,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> FileReadResult:
        self.targets.append(("read_file", project_id, branch))
        content = (self.root / path).read_text(encoding="utf-8").splitlines()
        final = len(content) if end_line is None else min(end_line, len(content))
        lines = tuple(
            FileLine(line=index, text=content[index - 1])
            for index in range(start_line, final + 1)
        )
        return FileReadResult(
            path=path,
            lines=lines,
            truncated=end_line is not None and end_line < len(content),
            content_hash=file_line_hash(lines),
        )

    async def list_directory(
        self,
        project_id: str,
        branch: str,
        *_args: object,
        **_kwargs: object,
    ) -> DirectoryListResult:
        self.targets.append(("list_directory", project_id, branch))
        return DirectoryListResult(path=".", entries=(), truncated=False)


async def agent_target(
    tmp_path: Path,
    model: SearchModelClient,
    **setting_overrides: object,
) -> tuple[SearchAgent, ProjectTools, str, str, Path]:
    settings = Settings(
        root=tmp_path / "agenthub",
        pricing_path=REPO_ROOT / "pricing.yaml",
        **cast(Any, setting_overrides),
    )
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir(parents=True)
    tools = ProjectTools(snapshot)
    return (
        SearchAgent(
            client=model,
            tools=cast(CodeSearchService, tools),
            settings=settings,
            prices=load_price_table(settings.pricing_path),
        ),
        tools,
        "project_demo",
        "feature/search",
        snapshot,
    )


async def test_multi_hop_answer_contains_only_read_citations(tmp_path: Path) -> None:
    model = RecordedModel(
        [
            tool_turn(
                (
                    "search_text",
                    {
                        "pattern": "recurring_discount",
                        "glob": "*.py",
                        "case_sensitive": True,
                        "literal": True,
                        "limit": 20,
                    },
                )
            ),
            tool_turn(
                (
                    "read_file",
                    {"path": "rules.py", "start_line": 1, "end_line": 4},
                )
            ),
            tool_turn(
                (
                    "read_file",
                    {"path": "customer.py", "start_line": 1, "end_line": 3},
                )
            ),
            tool_turn(
                (
                    "submit_answer",
                    {
                        "claims": [
                            {
                                "text": "Recurring customers receive ten percent off.",
                                "citations": [
                                    {"path": "rules.py", "line": 2, "end_line": 3}
                                ],
                            },
                            {
                                "text": "A customer is recurring after three orders.",
                                "citations": [
                                    {
                                        "path": "customer.py",
                                        "line": 1,
                                        "end_line": 2,
                                    }
                                ],
                            },
                        ]
                    },
                )
            ),
        ]
    )
    agent, tools, project_id, branch, snapshot = await agent_target(tmp_path, model)
    (snapshot / "rules.py").write_text(
        "def recurring_discount(customer):\n"
        "    if customer.is_recurring():\n"
        "        return 0.10\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (snapshot / "customer.py").write_text(
        "def is_recurring(customer):\n    return customer.order_count >= 3\n\n",
        encoding="utf-8",
    )
    try:
        result = await agent.answer(
            project_id, branch, "Explain the recurring discount rule"
        )
        assert result.complete is True
        assert len(result.claims) == 2
        assert result.claims[0].citations[0].content_hash == file_line_hash(
            (
                FileLine(2, "    if customer.is_recurring():"),
                FileLine(3, "        return 0.10"),
            )
        )
        assert [(span.path, span.line, span.end_line) for span in result.evidence] == [
            ("customer.py", 1, 3),
            ("rules.py", 1, 4),
        ]
        assert result.tool_calls == 4
        assert result.usage.counts.total == 60
        assert tools.targets[0] == ("validate_target", project_id, branch)
        assert sum(name == "validate_target" for name, _, _ in tools.targets) == 1
        assert {target[1:] for target in tools.targets} == {(project_id, branch)}
    finally:
        await agent.close()


async def test_unread_citation_is_rejected_instead_of_rendered(tmp_path: Path) -> None:
    model = RecordedModel(
        [
            tool_turn(
                (
                    "read_file",
                    {"path": "rule.py", "start_line": 1, "end_line": 2},
                )
            ),
            tool_turn(
                (
                    "submit_answer",
                    {
                        "claims": [
                            {
                                "text": "An unsupported claim.",
                                "citations": [
                                    {"path": "rule.py", "line": 9, "end_line": 9}
                                ],
                            }
                        ]
                    },
                )
            ),
            end_turn(),
        ]
    )
    agent, _tools, project_id, branch, snapshot = await agent_target(tmp_path, model)
    (snapshot / "rule.py").write_text("one\ntwo\n", encoding="utf-8")
    try:
        result = await agent.answer(project_id, branch, "Make a claim")
        assert result.complete is False
        assert result.claims == ()
        assert result.limit_reason is SearchLimitReason.MODEL_ENDED
        last_tool_result = model.messages[2][-1]
        assert "citation was not read" in str(last_tool_result)
    finally:
        await agent.close()


async def test_only_snapshot_correct_tools_are_offered_to_the_model(
    tmp_path: Path,
) -> None:
    model = RecordedModel([end_turn()])
    agent, _tools, project_id, branch, _snapshot = await agent_target(tmp_path, model)
    try:
        result = await agent.answer(project_id, branch, "Find the business rule")
        assert result.complete is False
        assert model.tool_names == [
            (
                "search_text",
                "search_structural",
                "read_file",
                "list_directory",
                "submit_answer",
            )
        ]
        assert "project branch snapshot" in model.system_prompts[0]
        assert "Semantic" not in model.system_prompts[0]
    finally:
        await agent.close()


async def test_invalid_project_branch_is_rejected_before_a_model_turn(
    tmp_path: Path,
) -> None:
    model = RecordedModel([end_turn()])
    agent, tools, project_id, branch, _snapshot = await agent_target(tmp_path, model)
    tools.target_error = SearchTargetNotFound("no such project branch")
    try:
        with pytest.raises(SearchTargetNotFound, match="no such project branch"):
            await agent.answer(project_id, branch, "Find the business rule")
        assert model.messages == []
    finally:
        await agent.close()


async def test_each_independent_ceiling_returns_a_partial_result(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "turns",
            {"search_max_turns": 1},
            [tool_turn(("list_directory", {"path": ".", "limit": 20}))],
            SearchLimitReason.TURNS,
        ),
        (
            "tool_calls",
            {"search_max_tool_calls": 1},
            [
                tool_turn(
                    ("read_file", {"path": "rule.py", "start_line": 1, "end_line": 1}),
                    ("list_directory", {"path": ".", "limit": 20}),
                )
            ],
            SearchLimitReason.TOOL_CALLS,
        ),
        (
            "bytes",
            {"search_max_bytes": 1_024},
            [
                tool_turn(
                    ("read_file", {"path": "large.py", "start_line": 1, "end_line": 1})
                )
            ],
            SearchLimitReason.BYTES,
        ),
        (
            "tokens",
            {"search_max_tokens": 1_024},
            [tool_turn(counts=TokenCounts(input_tokens=1_025))],
            SearchLimitReason.TOKENS,
        ),
    )
    for label, overrides, turns, expected in cases:
        model = RecordedModel(turns)
        agent, _tools, project_id, branch, snapshot = await agent_target(
            tmp_path / label, model, **overrides
        )
        (snapshot / "rule.py").write_text("rule\n", encoding="utf-8")
        (snapshot / "large.py").write_text("x" * 2_000 + "\n", encoding="utf-8")
        try:
            result = await agent.answer(project_id, branch, "Investigate")
            assert result.complete is False
            assert result.limit_reason is expected
            assert result.message
        finally:
            await agent.close()


class UncredentialedModel:
    """The SDK's behavior with no credential: a bare `TypeError`, on first use.

    Not a mock of our own interface — the point is the exact shape the real
    client raises, from a private `_validate_headers` hook with no exception
    type of its own.
    """

    def __init__(self, message: str) -> None:
        self.message = message

    async def complete(self, **_kwargs: object) -> ModelTurn:
        raise TypeError(self.message)

    async def close(self) -> None:
        pass


async def test_a_missing_credential_is_a_partial_result_not_a_500(
    tmp_path: Path,
) -> None:
    """Code search's chat has the same credential trap the planner had.

    `agent.py` caught `APIError` and not this, so a machine with no
    `ANTHROPIC_API_KEY` got an opaque 500 from `/api/search/answer`. Reported
    as `NOT_CONFIGURED` rather than `API_ERROR`: nothing was attempted, so
    "the API failed" would send the operator hunting for an outage.
    """
    model = UncredentialedModel(
        '"Could not resolve authentication method. Expected one of api_key, '
        'auth_token, or credentials to be set."'
    )
    agent, _tools, project_id, branch, _ = await agent_target(tmp_path, model)
    try:
        result = await agent.answer(project_id, branch, "Where is the scheduler?")

        assert result.complete is False
        assert result.limit_reason is SearchLimitReason.NOT_CONFIGURED
        assert "ANTHROPIC_API_KEY" in result.message
        assert result.usage.requests == 0
    finally:
        await agent.close()


async def test_an_unrelated_type_error_still_propagates_from_search(
    tmp_path: Path,
) -> None:
    """The catch is matched on a message, so it must stay narrow.

    A `TypeError` from anywhere else in the call is a bug in this module, and
    reporting it as "no credential" would send the operator looking for a key
    that is already set.
    """
    model = UncredentialedModel("complete() got an unexpected keyword argument")
    agent, _tools, project_id, branch, _ = await agent_target(tmp_path, model)
    try:
        with pytest.raises(TypeError, match="unexpected keyword"):
            await agent.answer(project_id, branch, "Where is the scheduler?")
    finally:
        await agent.close()


@dataclass(frozen=True)
class StructuredUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0


@dataclass(frozen=True)
class StructuredResult:
    data: dict[str, object]
    usage: StructuredUsage | None
    model: str


class RecordedCompleter:
    def __init__(self, results: Sequence[StructuredResult]) -> None:
        self.results = list(results)
        self.requests: list[HarnessSearchRequest] = []

    async def complete_structured(
        self, request: HarnessSearchRequest
    ) -> StructuredResult:
        self.requests.append(request)
        return self.results.pop(0)


def structured_action(action: str, **values: object) -> dict[str, object]:
    return {
        "action": action,
        "pattern": None,
        "glob": None,
        "case_sensitive": None,
        "literal": None,
        "limit": None,
        "language": None,
        "path": None,
        "start_line": None,
        "end_line": None,
        "claims": None,
        **values,
    }


def harness_client(
    completer: RecordedCompleter,
    *,
    model: str | None = "gpt-5.6-sol",
    max_transcript_bytes: int = 100_000,
) -> HarnessSearchClient[HarnessSearchRequest]:
    return create_harness_search_client(
        completer=completer,
        request_factory=lambda request: request,
        model=model,
        launcher=("ai-jail", "--clean"),
        max_transcript_bytes=max_transcript_bytes,
    )


async def test_harness_client_selects_one_backend_tool_without_a_cwd() -> None:
    completer = RecordedCompleter(
        [
            StructuredResult(
                data=structured_action(
                    "search_text",
                    pattern="scheduler",
                    glob="*.py",
                    case_sensitive=True,
                    literal=True,
                    limit=20,
                ),
                usage=StructuredUsage(11, 7, 5, 3),
                model="gpt-5.6-sol",
            )
        ]
    )
    client = harness_client(completer)

    turn = await client.complete(
        model="ignored-model",
        max_tokens=500,
        system="system",
        messages=[{"role": "user", "content": "Where is the scheduler?"}],
        tools=(),
    )

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "search_text"
    assert turn.tool_calls[0].input["pattern"] == "scheduler"
    assert turn.counts == TokenCounts(
        input_tokens=11,
        output_tokens=7,
        cache_read_tokens=5,
        cache_write_tokens=3,
    )
    assert turn.model == "gpt-5.6-sol"
    assert completer.requests[0].cwd is None
    assert completer.requests[0].launcher == ("ai-jail", "--clean")
    assert completer.requests[0].model == "gpt-5.6-sol"
    assert completer.requests[0].schema is HARNESS_ACTION_SCHEMA
    assert "snapshot" not in completer.requests[0].prompt


async def test_harness_client_preserves_the_cli_default_model() -> None:
    completer = RecordedCompleter(
        [
            StructuredResult(
                data=structured_action("list_directory", path=".", limit=20),
                usage=None,
                model="gpt-5.6-sol",
            )
        ]
    )
    client = harness_client(completer, model=None)

    await client.complete(
        model="claude-sonnet-5",
        max_tokens=500,
        system="system",
        messages=[{"role": "user", "content": "List the repository"}],
        tools=(),
    )

    assert completer.requests[0].model is None


async def test_harness_turns_execute_locally_then_submit_validated_evidence(
    tmp_path: Path,
) -> None:
    usage = StructuredUsage(
        input_tokens=10,
        output_tokens=5,
        cache_read_tokens=4,
        cache_write_tokens=3,
        cache_write_5m_tokens=3,
    )
    completer = RecordedCompleter(
        [
            StructuredResult(
                data=structured_action(
                    "read_file", path="rule.py", start_line=1, end_line=1
                ),
                usage=usage,
                model="gpt-5.6-sol",
            ),
            StructuredResult(
                data=structured_action(
                    "submit_answer",
                    claims=[
                        {
                            "text": "The scheduler runs hourly.",
                            "citations": [
                                {"path": "rule.py", "line": 1, "end_line": 1}
                            ],
                        }
                    ],
                ),
                usage=usage,
                model="gpt-5.6-sol",
            ),
        ]
    )
    client = harness_client(completer)
    agent, tools, project_id, branch, snapshot = await agent_target(tmp_path, client)
    (snapshot / "rule.py").write_text("scheduler runs hourly\n", encoding="utf-8")

    result = await agent.answer(project_id, branch, "When does it run?")

    assert result.complete is True
    assert result.claims[0].citations[0].path == "rule.py"
    assert result.usage.model == "gpt-5.6-sol"
    assert result.usage.counts == TokenCounts(
        input_tokens=20,
        output_tokens=10,
        cache_read_tokens=8,
        cache_write_tokens=6,
        cache_write_5m_tokens=6,
    )
    assert result.usage.cost_usd is not None
    assert result.usage.requests == 2
    assert len(completer.requests) == 2
    assert "tool_result" in completer.requests[1].prompt
    assert ("read_file", project_id, branch) in tools.targets


async def test_malformed_harness_action_becomes_a_safe_partial_result(
    tmp_path: Path,
) -> None:
    completer = RecordedCompleter(
        [
            StructuredResult(
                data=structured_action("read_file", path="rule.py"),
                usage=None,
                model="gpt-5.6-sol",
            )
        ]
    )
    agent, _tools, project_id, branch, _snapshot = await agent_target(
        tmp_path, harness_client(completer)
    )

    result = await agent.answer(project_id, branch, "Read the rule")

    assert result.complete is False
    assert result.limit_reason is SearchLimitReason.API_ERROR
    assert "SearchModelBackendError" in result.message
    assert result.usage.requests == 0


async def test_harness_client_enforces_capability_and_transcript_ceiling() -> None:
    with pytest.raises(ValueError, match="does not support structured output"):
        create_harness_search_client(
            completer=cast(Any, object()),
            request_factory=lambda request: request,
            model=None,
            launcher=(),
            max_transcript_bytes=100,
        )

    completer = RecordedCompleter([])
    client = harness_client(completer, max_transcript_bytes=10)
    with pytest.raises(SearchModelBackendError, match="byte ceiling"):
        await client.complete(
            model="gpt-5.6-sol",
            max_tokens=500,
            system="system",
            messages=[{"role": "user", "content": "question"}],
            tools=(),
        )
    assert completer.requests == []
