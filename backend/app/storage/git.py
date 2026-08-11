"""Bounded, read-only Git metadata and immutable branch snapshots.

Git writes belong to :mod:`app.orchestrator.worktree`. This module owns the
read side: repository discovery and commit-pinned source snapshots. It never
checks out a branch, updates a ref, or accepts a repository path from HTTP.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

MAX_LOCAL_BRANCHES = 4_000
MAX_ARCHIVE_BYTES = 128 * 1_024 * 1_024
MAX_SNAPSHOT_BYTES = 128 * 1_024 * 1_024
MAX_SNAPSHOT_ENTRIES = 50_000
MAX_GIT_STDERR_BYTES = 8_192
DEFAULT_GIT_READ_TIMEOUT_S = 15.0
_COMPLETE_FILE = "complete.json"
_TREE_DIRECTORY = "tree"
_ARCHIVE_PREFIX = f"{_TREE_DIRECTORY}/"


class GitReadError(Exception):
    """A bounded read-only Git operation failed."""


class GitUnavailableError(GitReadError):
    """The Git executable is unavailable."""


class GitReadTimedOut(GitReadError):
    """A Git read exceeded its fixed wall-clock bound."""


class GitMetadataTooLarge(GitReadError):
    """Repository metadata exceeded its fixed response bound."""


class GitSnapshotTooLarge(GitReadError):
    """A branch snapshot exceeded its byte or entry bound."""


class GitSnapshotUnsafe(GitReadError):
    """A Git archive contained an unsafe path or entry type."""


@dataclass(frozen=True, slots=True)
class LocalBranch:
    name: str
    ref: str
    commit: str
    is_head: bool


@dataclass(frozen=True, slots=True)
class GitRepository:
    root: Path
    common_dir: Path
    branches: tuple[LocalBranch, ...]


@dataclass(frozen=True, slots=True)
class GitSnapshot:
    root: Path
    commit: str


async def inspect_repository(
    repo_path: Path, *, timeout_s: float = DEFAULT_GIT_READ_TIMEOUT_S
) -> GitRepository:
    """Resolve repository identity and local branches from a known path."""
    identity = await _git_capture(
        repo_path,
        "rev-parse",
        "--path-format=absolute",
        "--show-toplevel",
        "--git-common-dir",
        timeout_s=timeout_s,
        operation="repository discovery",
    )
    lines = identity.decode("utf-8", errors="strict").splitlines()
    if len(lines) != 2:
        raise GitReadError(
            f"Git returned malformed repository identity for {repo_path}"
        )
    root, common_dir = await asyncio.to_thread(
        _resolve_repository_paths, Path(lines[0]), Path(lines[1])
    )
    branches = await list_local_branches(root, timeout_s=timeout_s)
    return GitRepository(root=root, common_dir=common_dir, branches=branches)


async def list_local_branches(
    repo_path: Path, *, timeout_s: float = DEFAULT_GIT_READ_TIMEOUT_S
) -> tuple[LocalBranch, ...]:
    raw = await _git_capture(
        repo_path,
        "for-each-ref",
        "--python",
        f"--count={MAX_LOCAL_BRANCHES + 1}",
        "--sort=refname",
        "--format=(%(refname), %(objectname), %(HEAD))",
        "refs/heads",
        timeout_s=timeout_s,
        operation="local branch listing",
    )
    branches = _parse_local_branches(raw, repo_path=repo_path)
    if len(branches) > MAX_LOCAL_BRANCHES:
        raise GitMetadataTooLarge(
            f"repository has more than {MAX_LOCAL_BRANCHES} local branches"
        )
    return branches


class GitSnapshotStore:
    """Materialize one immutable, atomically-published tree per commit."""

    def __init__(
        self,
        root: Path,
        *,
        timeout_s: float = DEFAULT_GIT_READ_TIMEOUT_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("Git snapshot timeout must be positive")
        self._root = root
        self._timeout_s = timeout_s
        self._locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def materialize(
        self,
        *,
        project_id: str,
        repository: Path,
        commit: str,
    ) -> GitSnapshot:
        if not _safe_project_id(project_id) or not _is_object_id(commit):
            raise GitSnapshotUnsafe("unsafe snapshot cache key")
        key = (project_id, commit)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            final = self._root / project_id / commit
            complete = await asyncio.to_thread(_complete_snapshot, final, commit)
            if complete:
                return GitSnapshot(root=final / _TREE_DIRECTORY, commit=commit)
            await asyncio.to_thread(_prepare_snapshot_parent, self._root, final)
            temporary = Path(
                await asyncio.to_thread(
                    tempfile.mkdtemp,
                    prefix=".build-",
                    dir=final.parent,
                )
            )
            try:
                archive = temporary / "archive.tar"
                tree = temporary / _TREE_DIRECTORY
                await _write_archive(
                    repository,
                    commit,
                    archive,
                    timeout_s=self._timeout_s,
                )
                await asyncio.to_thread(_extract_archive, archive, temporary)
                await asyncio.to_thread(archive.unlink)
                if not await asyncio.to_thread(tree.is_dir):
                    raise GitReadError("Git archive omitted its source root")
                await asyncio.to_thread(_make_tree_read_only, tree)
                await asyncio.to_thread(_write_complete_marker, temporary, commit)
                try:
                    await asyncio.to_thread(temporary.replace, final)
                except FileExistsError as error:
                    if not await asyncio.to_thread(_complete_snapshot, final, commit):
                        raise GitReadError(
                            "snapshot publication raced with an incomplete cache: "
                            f"{final}"
                        ) from error
                return GitSnapshot(root=final / _TREE_DIRECTORY, commit=commit)
            finally:
                if await asyncio.to_thread(temporary.exists):
                    await asyncio.to_thread(_remove_owned_tree, temporary, self._root)


async def _git_capture(
    repo_path: Path,
    *args: str,
    timeout_s: float,
    operation: str,
) -> bytes:
    if timeout_s <= 0:
        raise ValueError("Git read timeout must be positive")
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repo_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_environment(),
        )
    except FileNotFoundError as error:
        raise GitUnavailableError("Git executable is unavailable") from error
    try:
        async with asyncio.timeout(timeout_s):
            stdout, stderr = await process.communicate()
    except TimeoutError as error:
        await _stop(process)
        raise GitReadTimedOut(
            f"Git {operation} exceeded {timeout_s:g} seconds for {repo_path}"
        ) from error
    except BaseException:
        await _stop(process)
        raise
    if process.returncode != 0:
        detail = stderr[:MAX_GIT_STDERR_BYTES].decode("utf-8", errors="replace").strip()
        raise GitReadError(
            f"Git {operation} failed for {repo_path} "
            f"(exit {process.returncode}): {detail}"
        )
    return stdout


async def _write_archive(
    repository: Path,
    commit: str,
    destination: Path,
    *,
    timeout_s: float,
) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repository),
            "archive",
            "--format=tar",
            f"--prefix={_ARCHIVE_PREFIX}",
            "--",
            commit,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_git_environment(),
        )
    except FileNotFoundError as error:
        raise GitUnavailableError("Git executable is unavailable") from error
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(_read_stderr(process.stderr))
    handle: BinaryIO = await asyncio.to_thread(destination.open, "wb")
    size = 0
    try:
        async with asyncio.timeout(timeout_s):
            while chunk := await process.stdout.read(64 * 1_024):
                size += len(chunk)
                if size > MAX_ARCHIVE_BYTES:
                    raise GitSnapshotTooLarge(
                        f"Git archive exceeds {MAX_ARCHIVE_BYTES} bytes"
                    )
                await asyncio.to_thread(handle.write, chunk)
            return_code = await process.wait()
    except TimeoutError as error:
        await _stop(process)
        raise GitReadTimedOut(
            f"Git archive exceeded {timeout_s:g} seconds for commit {commit}"
        ) from error
    except BaseException:
        await _stop(process)
        raise
    finally:
        await asyncio.to_thread(handle.close)
        stderr = await stderr_task
    if return_code != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()
        raise GitReadError(
            f"Git archive failed for commit {commit} (exit {return_code}): {detail}"
        )


def _parse_local_branches(raw: bytes, *, repo_path: Path) -> tuple[LocalBranch, ...]:
    by_ref: dict[str, LocalBranch] = {}
    for encoded in raw.splitlines():
        try:
            value = ast.literal_eval(encoded.decode("utf-8"))
        except (SyntaxError, ValueError, UnicodeDecodeError) as error:
            raise GitReadError(
                f"Git returned malformed branch metadata for {repo_path}"
            ) from error
        if (
            not isinstance(value, tuple)
            or len(value) != 3
            or not all(isinstance(field, str) for field in value)
        ):
            raise GitReadError(
                f"Git returned malformed branch metadata for {repo_path}"
            )
        ref, commit, head = value
        name = _local_branch_name(ref)
        marker = head.strip()
        if name is None or not _is_object_id(commit) or marker not in {"", "*"}:
            raise GitReadError(f"Git returned unsafe branch metadata for {repo_path}")
        branch = LocalBranch(
            name=name,
            ref=ref,
            commit=commit,
            is_head=marker == "*",
        )
        previous = by_ref.setdefault(ref, branch)
        if previous != branch:
            raise GitReadError(
                f"Git returned conflicting metadata for branch {ref} in {repo_path}"
            )
    return tuple(
        sorted(
            by_ref.values(),
            key=lambda branch: (branch.name.casefold(), branch.name),
        )
    )


def _extract_archive(archive: Path, destination: Path) -> None:
    try:
        with tarfile.open(archive, mode="r:") as source:
            members = source.getmembers()
            # The fixed archive prefix contributes one directory member and
            # makes Git's otherwise header-only empty-tree TAR readable by
            # Python 3.13's tarfile module.
            if len(members) > MAX_SNAPSHOT_ENTRIES + 1:
                raise GitSnapshotTooLarge(
                    f"Git snapshot exceeds {MAX_SNAPSHOT_ENTRIES} entries"
                )
            total = 0
            for member in members:
                _validate_archive_member(member)
                if member.isfile():
                    total += member.size
                    if total > MAX_SNAPSHOT_BYTES:
                        raise GitSnapshotTooLarge(
                            f"Git snapshot exceeds {MAX_SNAPSHOT_BYTES} bytes"
                        )
            source.extractall(destination, members=members, filter="data")
    except (tarfile.TarError, OSError) as error:
        raise GitReadError(f"cannot extract Git archive: {error}") from error


def _validate_archive_member(member: tarfile.TarInfo) -> None:
    path = PurePosixPath(member.name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise GitSnapshotUnsafe(f"unsafe Git archive path: {member.name}")
    if not (member.isfile() or member.isdir() or member.issym()):
        raise GitSnapshotUnsafe(f"unsupported Git archive entry: {member.name}")
    if member.issym():
        link = PurePosixPath(member.linkname)
        combined = path.parent.joinpath(link)
        if link.is_absolute() or ".." in combined.parts:
            raise GitSnapshotUnsafe(
                f"Git archive symlink escapes snapshot: {member.name}"
            )


def _resolve_repository_paths(root: Path, common_dir: Path) -> tuple[Path, Path]:
    return root.resolve(strict=True), common_dir.resolve(strict=True)


def _prepare_snapshot_parent(cache_root: Path, final: Path) -> None:
    cache_root.mkdir(parents=True, exist_ok=True)
    resolved_cache = cache_root.resolve(strict=True)
    final.parent.mkdir(parents=True, exist_ok=True)
    resolved_parent = final.parent.resolve(strict=True)
    if resolved_parent.parent != resolved_cache:
        raise GitSnapshotUnsafe("snapshot path escapes cache root")
    if final.exists() or final.is_symlink():
        _remove_owned_tree(final, resolved_cache)


def _complete_snapshot(path: Path, commit: str) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    marker = path / _COMPLETE_FILE
    tree = path / _TREE_DIRECTORY
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return False
    return payload == {"commit": commit} and tree.is_dir() and not tree.is_symlink()


def _write_complete_marker(path: Path, commit: str) -> None:
    (path / _COMPLETE_FILE).write_text(
        json.dumps({"commit": commit}, sort_keys=True),
        encoding="utf-8",
    )


def _make_tree_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _remove_owned_tree(path: Path, cache_root: Path) -> None:
    resolved_root = cache_root.resolve(strict=True)
    parent = path.parent.resolve(strict=True)
    if parent != resolved_root and parent.parent != resolved_root:
        raise GitSnapshotUnsafe(
            f"refusing to remove path outside snapshot cache: {path}"
        )
    if path.is_symlink():
        path.unlink()
        return
    if path.is_dir():
        for child in path.rglob("*"):
            if not child.is_symlink() and child.is_dir():
                child.chmod(0o755)
        path.chmod(0o755)
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _local_branch_name(ref: str) -> str | None:
    prefix = "refs/heads/"
    if not ref.startswith(prefix):
        return None
    name = ref.removeprefix(prefix)
    if not name or any(
        ord(character) < 32 or ord(character) == 127 for character in name
    ):
        return None
    return name


def _is_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(
        character in "0123456789abcdef" for character in value
    )


def _safe_project_id(value: str) -> bool:
    return (
        value.startswith("proj_")
        and len(value) == 29
        and all(character in "0123456789abcdef" for character in value[5:])
    )


def project_id_for(common_dir: Path) -> str:
    digest = hashlib.sha256(os.fsencode(common_dir)).hexdigest()
    return f"proj_{digest[:24]}"


def _git_environment() -> dict[str, str]:
    return {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def _read_stderr(stream: asyncio.StreamReader) -> bytes:
    captured = bytearray()
    while chunk := await stream.read(4_096):
        remaining = MAX_GIT_STDERR_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured)


__all__ = [
    "GitMetadataTooLarge",
    "GitReadError",
    "GitReadTimedOut",
    "GitRepository",
    "GitSnapshot",
    "GitSnapshotStore",
    "GitSnapshotTooLarge",
    "GitSnapshotUnsafe",
    "GitUnavailableError",
    "LocalBranch",
    "inspect_repository",
    "list_local_branches",
    "project_id_for",
]
