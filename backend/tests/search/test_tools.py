"""E1 bounded code-search tools over an integration worktree."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models.ids import new_session_id
from app.search.tools import (
    CodeSearchService,
    InvalidSearchPattern,
    SearchPathError,
    SearchTimedOut,
    SearchToolUnavailable,
)
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository


async def search_service(
    tmp_path: Path, **overrides: object
) -> tuple[CodeSearchService, Database, str, Path]:
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
            title="Search target",
            repo_path=tmp_path / "repo",
            workspace_root=workspace,
            integration_branch=f"agenthub/{session_id}/integration",
        )
    return CodeSearchService(database, **overrides), database, session_id, integration


async def test_text_search_is_citable_filterable_and_bounded(tmp_path: Path) -> None:
    service, database, session_id, integration = await search_service(tmp_path)
    source = integration / "src"
    source.mkdir()
    (source / "rules.py").write_text(
        "def validate_tax_id(value):\n    return value.startswith('TAX-')\n",
        encoding="utf-8",
    )
    (source / "rules.ts").write_text(
        "export const validateTaxId = (value: string) => value.startsWith('tax-')\n",
        encoding="utf-8",
    )
    try:
        exact = await service.search_text(session_id, "validate_tax_id")
        assert exact.matches == (exact.matches[0],)
        assert exact.matches[0].path == "src/rules.py"
        assert (exact.matches[0].line, exact.matches[0].column) == (1, 5)

        literal = await service.search_text(
            session_id, "value.startswith('TAX-')", literal=True
        )
        assert [(match.path, match.line) for match in literal.matches] == [
            ("src/rules.py", 2)
        ]

        insensitive = await service.search_text(
            session_id, "TAX-", glob="*.ts", case_sensitive=False
        )
        assert [match.path for match in insensitive.matches] == ["src/rules.ts"]

        for index in range(4):
            (source / f"match_{index}.py").write_text("needle\n", encoding="utf-8")
        limited = await service.search_text(session_id, "needle", limit=2)
        assert len(limited.matches) == 2
        assert limited.truncated is True
        assert (await service.search_text(session_id, "absent")).matches == ()
    finally:
        await database.dispose()


async def test_invalid_pattern_and_missing_binary_are_typed(tmp_path: Path) -> None:
    service, database, session_id, _ = await search_service(tmp_path)
    try:
        with pytest.raises(InvalidSearchPattern):
            await service.search_text(session_id, "[")
        missing = CodeSearchService(database, rg_binary="definitely-no-such-rg")
        with pytest.raises(SearchToolUnavailable):
            await missing.search_text(session_id, "value")
    finally:
        await database.dispose()


async def test_timeout_kills_the_search_process(tmp_path: Path) -> None:
    script = tmp_path / "slow-rg"
    script.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8")
    script.chmod(0o755)
    service, database, session_id, _ = await search_service(
        tmp_path / "runtime", rg_binary=str(script), timeout_s=0.01
    )
    try:
        with pytest.raises(SearchTimedOut):
            await service.search_text(session_id, "value")
    finally:
        await database.dispose()


async def test_file_and_directory_reads_cannot_escape_the_worktree(
    tmp_path: Path,
) -> None:
    service, database, session_id, integration = await search_service(tmp_path)
    source = integration / "src"
    source.mkdir()
    (source / "rules.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (source / "escape").symlink_to(outside)
    try:
        read = await service.read_file(
            session_id, "src/rules.py", start_line=2, end_line=3
        )
        assert [(line.line, line.text) for line in read.lines] == [
            (2, "two"),
            (3, "three"),
        ]
        listing = await service.list_directory(session_id, "src")
        assert [(entry.path, entry.kind) for entry in listing.entries] == [
            ("src/rules.py", "file")
        ]
        with pytest.raises(SearchPathError):
            await service.read_file(session_id, "src/escape")
        with pytest.raises(SearchPathError):
            await service.read_file(session_id, "../secret.txt")
        with pytest.raises(ValueError, match="400 lines"):
            await service.read_file(session_id, "src/rules.py", end_line=401)

        moved = integration.with_name("integration-original")
        integration.rename(moved)
        integration.symlink_to(tmp_path)
        with pytest.raises(SearchPathError, match="escapes its workspace"):
            await service.list_directory(session_id)
    finally:
        await database.dispose()
