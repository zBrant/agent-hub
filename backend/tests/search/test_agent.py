"""E4 bounded agentic search with an evidence ledger."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path

from anthropic.types import MessageParam, ToolParam

from app.config import Settings
from app.models.ids import new_session_id
from app.models.pricing import TokenCounts, load_price_table
from app.search.agent import (
    ModelToolCall,
    ModelTurn,
    SearchAgent,
    SearchLimitReason,
)
from app.search.symbols import SymbolIndexService
from app.search.tools import CodeSearchService, FileLine, file_line_hash
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]


class RecordedModel:
    def __init__(self, turns: Sequence[ModelTurn]) -> None:
        self.turns = list(turns)
        self.messages: list[Sequence[MessageParam]] = []
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


async def agent_target(
    tmp_path: Path,
    model: RecordedModel,
    **setting_overrides: object,
) -> tuple[SearchAgent, Database, str, Path]:
    settings = Settings(
        root=tmp_path / "agenthub",
        pricing_path=REPO_ROOT / "pricing.yaml",
        **setting_overrides,
    )
    upgrade_database_sync(settings.database_url)
    database = Database.from_settings(settings)
    session_id = new_session_id()
    workspace = settings.workspaces_root / session_id
    integration = workspace / "integration"
    integration.mkdir(parents=True)
    async with database.session() as db_session:
        await Repository(db_session).create_session(
            session_id=session_id,
            title="Agent search",
            repo_path=tmp_path / "repo",
            workspace_root=workspace,
            integration_branch=f"agenthub/{session_id}/integration",
        )
    tools = CodeSearchService(database)
    symbols = SymbolIndexService(database)
    return (
        SearchAgent(
            client=model,
            tools=tools,
            symbols=symbols,
            settings=settings,
            prices=load_price_table(settings.pricing_path),
        ),
        database,
        session_id,
        integration,
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
    agent, database, session_id, integration = await agent_target(tmp_path, model)
    (integration / "rules.py").write_text(
        "def recurring_discount(customer):\n"
        "    if customer.is_recurring():\n"
        "        return 0.10\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (integration / "customer.py").write_text(
        "def is_recurring(customer):\n    return customer.order_count >= 3\n\n",
        encoding="utf-8",
    )
    try:
        result = await agent.answer(session_id, "Explain the recurring discount rule")
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
    finally:
        await agent.close()
        await database.dispose()


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
    agent, database, session_id, integration = await agent_target(tmp_path, model)
    (integration / "rule.py").write_text("one\ntwo\n", encoding="utf-8")
    try:
        result = await agent.answer(session_id, "Make a claim")
        assert result.complete is False
        assert result.claims == ()
        assert result.limit_reason is SearchLimitReason.MODEL_ENDED
        last_tool_result = model.messages[2][-1]
        assert "citation was not read" in str(last_tool_result)
    finally:
        await agent.close()
        await database.dispose()


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
        agent, database, session_id, integration = await agent_target(
            tmp_path / label, model, **overrides
        )
        (integration / "rule.py").write_text("rule\n", encoding="utf-8")
        (integration / "large.py").write_text("x" * 2_000 + "\n", encoding="utf-8")
        try:
            result = await agent.answer(session_id, "Investigate")
            assert result.complete is False
            assert result.limit_reason is expected
            assert result.message
        finally:
            await agent.close()
            await database.dispose()
