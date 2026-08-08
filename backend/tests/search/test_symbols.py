"""E3 incremental Tree-sitter symbol index."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlmodel import col, select

from app.models.ids import new_session_id
from app.models.tables import SymbolSource
from app.search.symbols import (
    ExtractedSymbol,
    SymbolIndexManager,
    SymbolIndexService,
    TreeSitterSymbolExtractor,
)
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository


class CountingExtractor:
    def __init__(self) -> None:
        self.inner = TreeSitterSymbolExtractor()
        self.paths = 0

    def language_for_path(self, path: str) -> str | None:
        return self.inner.language_for_path(path)

    def extract(self, language: str, source: bytes) -> tuple[ExtractedSymbol, ...]:
        self.paths += 1
        return self.inner.extract(language, source)


async def index_target(tmp_path: Path) -> tuple[Database, str, Path]:
    root = tmp_path / "agenthub"
    database_url = f"sqlite+aiosqlite:///{root / 'agenthub.db'}"
    upgrade_database_sync(database_url)
    database = Database(database_url)
    session_id = new_session_id()
    workspace = root / "workspaces" / session_id
    integration = workspace / "integration"
    integration.mkdir(parents=True)
    async with database.session() as db_session:
        await Repository(db_session).create_session(
            session_id=session_id,
            title="Symbol target",
            repo_path=tmp_path / "repo",
            workspace_root=workspace,
            integration_branch=f"agenthub/{session_id}/integration",
        )
    return database, session_id, integration


async def test_duplicate_definitions_are_citable_and_restart_reuses_hashes(
    tmp_path: Path,
) -> None:
    database, session_id, integration = await index_target(tmp_path)
    (integration / "a.py").write_text(
        "def validate(value):\n    return normalize(value)\n", encoding="utf-8"
    )
    (integration / "b.py").write_text(
        "def validate(value):\n    return value\n", encoding="utf-8"
    )
    extractor = CountingExtractor()
    service = SymbolIndexService(database, extractor=extractor)
    try:
        assert await service.sync_session(session_id, integration) == 2
        assert extractor.paths == 2
        definitions = await service.find_symbol(session_id, "validate")
        assert [
            (match.path, match.line, match.column, match.kind)
            for match in definitions.matches
        ] == [
            ("a.py", 1, 5, "function"),
            ("b.py", 1, 5, "function"),
        ]
        references = await service.find_references(session_id, "normalize")
        assert [(match.path, match.line) for match in references.matches] == [
            ("a.py", 2)
        ]

        restarted_extractor = CountingExtractor()
        restarted = SymbolIndexService(database, extractor=restarted_extractor)
        assert await restarted.sync_session(session_id, integration) == 0
        assert restarted_extractor.paths == 0
        async with database.session() as db_session:
            sources = (
                await db_session.exec(
                    select(SymbolSource).where(
                        col(SymbolSource.session_id) == session_id
                    )
                )
            ).all()
        assert len(sources) == 2
        assert all(len(source.source_hash) == 64 for source in sources)
    finally:
        await database.dispose()


async def test_sync_reindexes_only_changed_files_and_removes_deleted_rows(
    tmp_path: Path,
) -> None:
    database, session_id, integration = await index_target(tmp_path)
    first = integration / "first.py"
    second = integration / "second.py"
    first.write_text("def first(): pass\n", encoding="utf-8")
    second.write_text("def second(): pass\n", encoding="utf-8")
    extractor = CountingExtractor()
    service = SymbolIndexService(database, extractor=extractor)
    try:
        assert await service.sync_session(session_id, integration) == 2
        extractor.paths = 0

        first.write_text("def changed(): pass\n", encoding="utf-8")
        second.unlink()
        (integration / "third.ts").write_text(
            "export function third(): void {}\n", encoding="utf-8"
        )
        assert await service.sync_session(session_id, integration) == 3
        assert extractor.paths == 2
        assert (await service.find_symbol(session_id, "first")).matches == ()
        assert (await service.find_symbol(session_id, "second")).matches == ()
        assert (await service.find_symbol(session_id, "changed")).matches
        assert (await service.find_symbol(session_id, "third")).matches
    finally:
        await database.dispose()


async def test_watch_manager_applies_create_change_and_delete_events(
    tmp_path: Path,
) -> None:
    database, session_id, integration = await index_target(tmp_path)
    source = integration / "watched.py"
    source.write_text("def before(): pass\n", encoding="utf-8")
    service = SymbolIndexService(database)
    manager = SymbolIndexManager(database, service, discovery_interval_s=0.01)
    manager.start()

    async def wait_for(name: str, *, present: bool) -> None:
        async with asyncio.timeout(5):
            for _ in range(250):
                found = bool((await service.find_symbol(session_id, name)).matches)
                if found is present:
                    return
                await asyncio.sleep(0.02)
        raise AssertionError(f"symbol presence did not become {present}: {name}")

    try:
        await wait_for("before", present=True)
        source.write_text("def after(): pass\n", encoding="utf-8")
        await wait_for("after", present=True)
        await wait_for("before", present=False)
        source.unlink()
        await wait_for("after", present=False)
    finally:
        await manager.close()
        await database.dispose()
