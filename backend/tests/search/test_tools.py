"""Bounded code-search tools over project branch snapshots."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.models.ids import new_session_id
from app.search.tools import (
    CodeSearchService,
    InvalidSearchPattern,
    InvalidStructuralPattern,
    SearchPathError,
    SearchTargetNotFound,
    SearchTimedOut,
    SearchToolUnavailable,
)
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository


async def git(cwd: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()
    return output.decode().strip()


async def commit_all(repo: Path, message: str = "fixture") -> None:
    await git(repo, "add", "-A")
    await git(repo, "commit", "-qm", message)


async def search_service(
    tmp_path: Path, **overrides: object
) -> tuple[CodeSearchService, Database, str, str, Path]:
    root = tmp_path / "agenthub"
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    await git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("initial\n", encoding="utf-8")
    await commit_all(repo, "initial")

    database_url = f"sqlite+aiosqlite:///{root / 'agenthub.db'}"
    upgrade_database_sync(database_url)
    database = Database(database_url)
    session_id = new_session_id()
    async with database.session() as db_session:
        await Repository(db_session).create_session(
            session_id=session_id,
            title="Project discovery source",
            repo_path=repo,
            workspace_root=root / "workspaces" / session_id,
            integration_branch=f"agenthub/{session_id}/integration",
        )
    service = CodeSearchService(
        database,
        snapshots_root=root / "search-snapshots",
        **overrides,
    )
    catalog = await service.list_projects()
    assert len(catalog.projects) == 1
    return service, database, catalog.projects[0].id, "main", repo


async def test_text_search_is_citable_filterable_and_bounded(tmp_path: Path) -> None:
    service, database, project_id, branch, repo = await search_service(tmp_path)
    source = repo / "src"
    source.mkdir()
    (source / "rules.py").write_text(
        "def validate_tax_id(value):\n    return value.startswith('TAX-')\n",
        encoding="utf-8",
    )
    (source / "rules.ts").write_text(
        "export const validateTaxId = (value: string) => value.startsWith('tax-')\n",
        encoding="utf-8",
    )
    for index in range(4):
        (source / f"match_{index}.py").write_text("needle\n", encoding="utf-8")
    await commit_all(repo)
    try:
        exact = await service.search_text(project_id, branch, "validate_tax_id")
        assert exact.matches == (exact.matches[0],)
        assert exact.matches[0].path == "src/rules.py"
        assert (exact.matches[0].line, exact.matches[0].column) == (1, 5)

        literal = await service.search_text(
            project_id,
            branch,
            "value.startswith('TAX-')",
            literal=True,
        )
        assert [(match.path, match.line) for match in literal.matches] == [
            ("src/rules.py", 2)
        ]

        insensitive = await service.search_text(
            project_id,
            branch,
            "TAX-",
            glob="*.ts",
            case_sensitive=False,
        )
        assert [match.path for match in insensitive.matches] == ["src/rules.ts"]
        limited = await service.search_text(project_id, branch, "needle", limit=2)
        assert len(limited.matches) == 2
        assert limited.truncated is True
        assert (await service.search_text(project_id, branch, "absent")).matches == ()
    finally:
        await database.dispose()


async def test_invalid_pattern_and_missing_binary_are_typed(tmp_path: Path) -> None:
    service, database, project_id, branch, _ = await search_service(tmp_path)
    try:
        with pytest.raises(InvalidSearchPattern):
            await service.search_text(project_id, branch, "[")
        missing = CodeSearchService(
            database,
            rg_binary="definitely-no-such-rg",
            snapshots_root=tmp_path / "snapshots",
        )
        with pytest.raises(SearchToolUnavailable):
            await missing.search_text(project_id, branch, "value")
    finally:
        await database.dispose()


async def test_structural_search_uses_streaming_json_citations_and_bounds(
    tmp_path: Path,
) -> None:
    script = tmp_path / "fake-sg"
    script.write_text(
        """#!/usr/bin/env python3
import json
import sys

expected = [
    "run", "--pattern", "logger.$METHOD($ARG)", "--lang", "python",
    "--json=stream", ".",
]
if sys.argv[1:] != expected:
    print("unexpected argv", file=sys.stderr)
    raise SystemExit(9)
for line, column in ((3, 8), (7, 4)):
    print(json.dumps({
        "text": "logger.info(value)",
        "range": {
            "start": {"line": line, "column": column},
            "end": {"line": line, "column": column + 18},
        },
        "file": "./src/service.py",
        "lines": "logger.info(value)",
    }))
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    service, database, project_id, branch, _ = await search_service(
        tmp_path / "runtime", sg_binary=str(script)
    )
    try:
        result = await service.search_structural(
            project_id,
            branch,
            "logger.$METHOD($ARG)",
            language="python",
            limit=1,
        )
        assert result.truncated is True
        assert result.matches[0].path == "src/service.py"
        assert (result.matches[0].line, result.matches[0].column) == (4, 9)
    finally:
        await database.dispose()


async def test_structural_errors_and_missing_capability_are_typed(
    tmp_path: Path,
) -> None:
    script = tmp_path / "invalid-sg"
    script.write_text(
        "#!/bin/sh\nprintf 'invalid language or pattern' >&2\nexit 2\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    service, database, project_id, branch, _ = await search_service(
        tmp_path / "runtime", sg_binary=str(script)
    )
    try:
        with pytest.raises(
            InvalidStructuralPattern, match="invalid language or pattern"
        ):
            await service.search_structural(
                project_id, branch, "$A", language="unknown"
            )
        missing = CodeSearchService(
            database,
            sg_binary="definitely-no-such-sg",
            snapshots_root=tmp_path / "snapshots",
        )
        with pytest.raises(SearchToolUnavailable, match="structural search"):
            await missing.search_structural(project_id, branch, "$A", language="python")
    finally:
        await database.dispose()


async def test_timeout_kills_the_search_process(tmp_path: Path) -> None:
    script = tmp_path / "slow-rg"
    script.write_text("#!/bin/sh\nsleep 1\n", encoding="utf-8")
    script.chmod(0o755)
    service, database, project_id, branch, _ = await search_service(
        tmp_path / "runtime",
        rg_binary=str(script),
        timeout_s=0.01,
    )
    try:
        with pytest.raises(SearchTimedOut):
            await service.search_text(project_id, branch, "value")
    finally:
        await database.dispose()


async def test_file_and_directory_reads_are_snapshot_scoped(tmp_path: Path) -> None:
    service, database, project_id, branch, repo = await search_service(tmp_path)
    source = repo / "src"
    source.mkdir()
    rules = source / "rules.py"
    rules.write_text("one\ntwo\nthree\n", encoding="utf-8")
    (source / "rules-link.py").symlink_to("rules.py")
    await commit_all(repo)
    try:
        read = await service.read_file(
            project_id, branch, "src/rules.py", start_line=2, end_line=3
        )
        assert [(line.line, line.text) for line in read.lines] == [
            (2, "two"),
            (3, "three"),
        ]
        original_hash = read.content_hash

        rules.write_text("one\nchanged\nthree\n", encoding="utf-8")
        await commit_all(repo, "changed")
        changed = await service.read_file(
            project_id, branch, "src/rules.py", start_line=2, end_line=3
        )
        assert changed.content_hash != original_hash
        linked = await service.read_file(project_id, branch, "src/rules-link.py")
        assert linked.lines[1].text == "changed"
        listing = await service.list_directory(project_id, branch, "src")
        assert [(entry.path, entry.kind) for entry in listing.entries] == [
            ("src/rules-link.py", "file"),
            ("src/rules.py", "file"),
        ]
        with pytest.raises(SearchPathError):
            await service.read_file(project_id, branch, "../secret.txt")
        with pytest.raises(ValueError, match="400 lines"):
            await service.read_file(project_id, branch, "src/rules.py", end_line=401)
    finally:
        await database.dispose()


async def test_project_catalog_deduplicates_known_repositories(tmp_path: Path) -> None:
    service, database, project_id, _, repo = await search_service(tmp_path)
    await git(repo, "branch", "Zebra")
    await git(repo, "branch", "alpha")
    session_ulid = "01KZQ21NPNPWGWKRAG3SHPWZ9T"
    node_ulid = "01KZQ21NPNPWGWKRAG3SHPWZ9P"
    await git(repo, "branch", f"agenthub/sess_{session_ulid}/integration")
    await git(repo, "branch", f"agenthub/sess_{session_ulid}/node_{node_ulid}")
    await git(repo, "branch", f"agenthub/sess_{session_ulid}/result")
    await git(repo, "update-ref", "refs/heads/feature;touch-owned", "HEAD")
    alias = tmp_path / "repo-alias"
    alias.symlink_to(repo, target_is_directory=True)
    async with database.session() as db_session:
        await Repository(db_session).create_session(
            session_id=new_session_id(),
            title="Same project through symlink",
            repo_path=alias,
            workspace_root=tmp_path / "unused",
            integration_branch="unused",
        )
    try:
        catalog = await service.list_projects()
        assert len(catalog.projects) == 1
        project = catalog.projects[0]
        assert project.id == project_id
        assert project.id.startswith("proj_")
        assert not project.id.startswith("sess_")
        names = [branch.name for branch in project.branches]
        assert names == sorted(names, key=lambda name: (name.casefold(), name))
        assert len(names) == len(set(names))
        assert "feature;touch-owned" in names
        assert f"agenthub/sess_{session_ulid}/integration" not in names
        assert f"agenthub/sess_{session_ulid}/node_{node_ulid}" not in names
        assert f"agenthub/sess_{session_ulid}/result" in names
        assert not (tmp_path / "owned").exists()
        assert next(
            branch for branch in project.branches if branch.name == "main"
        ).is_head
    finally:
        await database.dispose()


async def test_branch_snapshots_are_isolated_and_pinned_per_agent_turn(
    tmp_path: Path,
) -> None:
    service, database, project_id, _, repo = await search_service(tmp_path)
    feature_tree = tmp_path / "feature-tree"
    await git(repo, "worktree", "add", "-qb", "feature", str(feature_tree), "main")
    target = feature_tree / "value.txt"
    target.write_text("feature one\n", encoding="utf-8")
    await commit_all(feature_tree, "feature one")
    (repo / "dirty.txt").write_text("not committed\n", encoding="utf-8")
    try:
        await service.validate_target(project_id, "feature")
        first = await service.read_file(project_id, "feature", "value.txt")
        assert first.lines[0].text == "feature one"

        target.write_text("feature two\n", encoding="utf-8")
        await commit_all(feature_tree, "feature two")
        pinned = await service.read_file(project_id, "feature", "value.txt")
        assert pinned.lines[0].text == "feature one"

        fresh = CodeSearchService(
            database,
            snapshots_root=tmp_path / "agenthub" / "search-snapshots",
        )
        latest = await fresh.read_file(project_id, "feature", "value.txt")
        assert latest.lines[0].text == "feature two"
        assert await git(repo, "branch", "--show-current") == "main"
        with pytest.raises(SearchTargetNotFound):
            await fresh.read_file(project_id, "feature", "dirty.txt")
        with pytest.raises(SearchTargetNotFound, match="no such project"):
            await fresh.validate_target("proj_000000000000000000000000", "main")
        with pytest.raises(SearchTargetNotFound, match="no local branch"):
            await fresh.validate_target(project_id, "refs/heads/feature")
    finally:
        await database.dispose()
