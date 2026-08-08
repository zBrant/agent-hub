#!/usr/bin/env python
"""Run deterministic Phase 4 acceptance against an integration worktree."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from sqlmodel import col, select

from app.config import Settings
from app.models.ids import new_session_id
from app.models.tables import CodeSymbol
from app.search.semantic import SemanticIndexService
from app.search.symbols import SymbolIndexService
from app.search.tools import CodeSearchService
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository


async def accept(integration: Path, revision: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agenthub-phase4-") as raw_root:
        settings = Settings(root=Path(raw_root))
        upgrade_database_sync(settings.database_url)
        database = Database.from_settings(settings)
        session_id = new_session_id()
        workspace = integration.parent
        async with database.session() as db_session:
            await Repository(db_session).create_session(
                session_id=session_id,
                title="Phase 4 acceptance",
                repo_path=integration,
                workspace_root=workspace,
                integration_branch="detached-acceptance",
            )
        symbols = SymbolIndexService(database)
        semantic = SemanticIndexService(database)
        tools = CodeSearchService(database)
        try:
            symbol_changes = await symbols.sync_session(session_id, integration)
            semantic_changes = await semantic.sync_session(session_id, integration)
            reused = await semantic.sync_session(session_id, integration)

            async with database.session() as db_session:
                indexed_definition = (
                    await db_session.exec(
                        select(CodeSymbol)
                        .where(
                            col(CodeSymbol.session_id) == session_id,
                            col(CodeSymbol.path) == "backend/app/search/agent.py",
                            col(CodeSymbol.role) == "definition",
                        )
                        .order_by(col(CodeSymbol.start_line))
                    )
                ).first()
            if indexed_definition is None:
                raise RuntimeError("agent.py has no indexed definition")
            symbol = await symbols.find_symbol(
                session_id, indexed_definition.name, limit=5
            )
            if not symbol.matches:
                raise RuntimeError("indexed definition was not found by name")
            definition = symbol.matches[0]
            source = await tools.read_file(
                session_id,
                definition.path,
                start_line=definition.line,
                end_line=definition.end_line,
            )

            cases = (
                (
                    "evidence citation read file validated answer",
                    "backend/app/search/agent.py",
                ),
                (
                    "incremental symbol tree sitter tags parser",
                    "backend/app/search/symbols.py",
                ),
            )
            searches: list[dict[str, object]] = []
            for query, expected_path in cases:
                result = await semantic.search(session_id, query, limit=5)
                paths = [match.path for match in result.matches]
                if expected_path not in paths:
                    raise RuntimeError(
                        f"{expected_path} absent from semantic top five for {query!r}"
                    )
                searches.append(
                    {
                        "query": query,
                        "expected_path": expected_path,
                        "rank": paths.index(expected_path) + 1,
                        "top_paths": paths,
                    }
                )
            return {
                "revision": revision,
                "symbol_files_indexed": symbol_changes,
                "semantic_files_indexed": semantic_changes,
                "unchanged_files_reindexed": reused,
                "symbol_citation": {
                    "name": indexed_definition.name,
                    "path": source.path,
                    "line": source.lines[0].line,
                    "end_line": source.lines[-1].line,
                    "content_hash": source.content_hash,
                },
                "semantic_searches": searches,
            }
        finally:
            await database.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("integration", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    result = asyncio.run(accept(args.integration.resolve(strict=True), args.revision))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
