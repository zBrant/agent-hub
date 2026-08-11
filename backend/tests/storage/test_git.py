"""Read-only Git discovery and atomic snapshot cache contracts."""

from __future__ import annotations

import asyncio
import stat
from pathlib import Path

import pytest

import app.storage.git as git_storage
from app.storage.git import (
    GitSnapshotStore,
    GitSnapshotTooLarge,
    GitSnapshotUnsafe,
    inspect_repository,
    project_id_for,
)


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


async def repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    await git(root, "init", "-q", "-b", "main")
    (root / "tracked.txt").write_text("committed\n", encoding="utf-8")
    await git(root, "add", "tracked.txt")
    await git(root, "commit", "-qm", "initial")
    return root


async def test_snapshot_is_commit_pinned_atomic_read_only_and_concurrent(
    tmp_path: Path,
) -> None:
    repo = await repository(tmp_path)
    metadata = await inspect_repository(repo)
    project_id = project_id_for(metadata.common_dir)
    commit = next(
        branch.commit for branch in metadata.branches if branch.name == "main"
    )
    (repo / "uncommitted.txt").write_text("working tree only\n", encoding="utf-8")
    store = GitSnapshotStore(tmp_path / "cache")

    snapshots = await asyncio.gather(
        *(
            store.materialize(project_id=project_id, repository=repo, commit=commit)
            for _ in range(8)
        )
    )
    assert {snapshot.root for snapshot in snapshots} == {snapshots[0].root}
    root = snapshots[0].root
    assert (root / "tracked.txt").read_text(encoding="utf-8") == "committed\n"
    assert not (root / "uncommitted.txt").exists()
    assert not stat.S_IMODE((root / "tracked.txt").stat().st_mode) & stat.S_IWUSR
    assert await git(repo, "branch", "--show-current") == "main"
    assert not list(root.parent.parent.glob(".build-*"))
    assert (root.parent / "complete.json").is_file()


async def test_snapshot_materializes_an_empty_commit(tmp_path: Path) -> None:
    repo = tmp_path / "empty-repo"
    repo.mkdir()
    await git(repo, "init", "-q", "-b", "main")
    await git(repo, "commit", "--allow-empty", "-qm", "empty root")
    metadata = await inspect_repository(repo)
    branch = next(branch for branch in metadata.branches if branch.name == "main")
    store = GitSnapshotStore(tmp_path / "cache")

    snapshot = await store.materialize(
        project_id=project_id_for(metadata.common_dir),
        repository=repo,
        commit=branch.commit,
    )

    assert snapshot.root.is_dir()
    assert list(snapshot.root.iterdir()) == []
    assert not stat.S_IMODE(snapshot.root.stat().st_mode) & stat.S_IWUSR


async def test_snapshot_rejects_escaping_symlink_and_cleans_partial_cache(
    tmp_path: Path,
) -> None:
    repo = await repository(tmp_path)
    (repo / "escape").symlink_to(tmp_path / "secret")
    await git(repo, "add", "escape")
    await git(repo, "commit", "-qm", "unsafe link")
    metadata = await inspect_repository(repo)
    branch = next(branch for branch in metadata.branches if branch.name == "main")
    project_id = project_id_for(metadata.common_dir)
    cache = tmp_path / "cache"
    store = GitSnapshotStore(cache)

    with pytest.raises(GitSnapshotUnsafe, match="symlink escapes"):
        await store.materialize(
            project_id=project_id,
            repository=repo,
            commit=branch.commit,
        )
    project_cache = cache / project_id
    assert not (project_cache / branch.commit).exists()
    assert not list(project_cache.glob(".build-*"))


async def test_snapshot_archive_byte_bound_cleans_partial_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = await repository(tmp_path)
    metadata = await inspect_repository(repo)
    branch = next(branch for branch in metadata.branches if branch.name == "main")
    project_id = project_id_for(metadata.common_dir)
    cache = tmp_path / "cache"
    monkeypatch.setattr(git_storage, "MAX_ARCHIVE_BYTES", 1)
    store = GitSnapshotStore(cache)

    with pytest.raises(GitSnapshotTooLarge, match="archive exceeds"):
        await store.materialize(
            project_id=project_id,
            repository=repo,
            commit=branch.commit,
        )
    project_cache = cache / project_id
    assert not (project_cache / branch.commit).exists()
    assert not list(project_cache.glob(".build-*"))
