"""Bounded lexical and filesystem tools over immutable branch snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Callable, Sequence
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from sqlmodel import col, select

from app.models.tables import Session
from app.storage.db import Database, database_path
from app.storage.git import (
    GitMetadataTooLarge,
    GitReadError,
    GitReadTimedOut,
    GitRepository,
    GitSnapshot,
    GitSnapshotStore,
    GitSnapshotTooLarge,
    GitSnapshotUnsafe,
    GitUnavailableError,
    inspect_repository,
    project_id_for,
)

MAX_RESULTS = 200
MAX_STDERR_BYTES = 8_192
MAX_PREVIEW_CHARS = 500
MAX_FILE_LINES = 400
MAX_FILE_BYTES = 128 * 1_024
MAX_SCAN_BYTES = 2 * 1_024 * 1_024
MAX_DIRECTORY_ENTRIES = 500
DEFAULT_TIMEOUT_S = 5.0
_ULID_PATTERN = r"[0-9A-HJKMNP-TV-Z]{26}"
_INTERNAL_EXECUTION_BRANCH = re.compile(
    rf"agenthub/sess_{_ULID_PATTERN}/(?:integration|node_{_ULID_PATTERN})"
)


class SearchError(Exception):
    """Base error for a bounded code-search operation."""


class SearchTargetNotFound(SearchError):
    """The project, branch snapshot, or requested path does not exist."""


class SearchPathError(SearchError):
    """A requested path is absolute or escapes the branch snapshot."""


class SearchToolUnavailable(SearchError):
    """A required local search executable is not installed."""


class InvalidSearchPattern(SearchError):
    """ripgrep rejected the supplied regular expression or glob."""


class InvalidStructuralPattern(SearchError):
    """ast-grep rejected the supplied language or structural pattern."""


class SearchTimedOut(SearchError):
    """A search exceeded its fixed wall-clock budget."""


class SearchOutputTooLarge(SearchError):
    """A search result or branch snapshot exceeded a fixed bound."""


@dataclass(frozen=True, slots=True)
class TextMatch:
    path: str
    line: int
    column: int
    preview: str


@dataclass(frozen=True, slots=True)
class TextSearchResult:
    matches: tuple[TextMatch, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileLine:
    line: int
    text: str


@dataclass(frozen=True, slots=True)
class FileReadResult:
    path: str
    lines: tuple[FileLine, ...]
    truncated: bool
    content_hash: str


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: str
    kind: str


@dataclass(frozen=True, slots=True)
class DirectoryListResult:
    path: str
    entries: tuple[DirectoryEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class ProjectBranch:
    name: str
    commit: str
    is_head: bool


@dataclass(frozen=True, slots=True)
class ProjectTarget:
    id: str
    name: str
    repo_path: Path
    branches: tuple[ProjectBranch, ...]


@dataclass(frozen=True, slots=True)
class ProjectCatalogResult:
    projects: tuple[ProjectTarget, ...]


@dataclass(frozen=True, slots=True)
class _ProjectRecord:
    target: ProjectTarget
    repository: GitRepository


class CodeSearchService:
    """Resolve a known project and exact local branch to a read-only snapshot."""

    def __init__(
        self,
        database: Database,
        *,
        rg_binary: str = "rg",
        sg_binary: str = "sg",
        timeout_s: float = DEFAULT_TIMEOUT_S,
        snapshots_root: Path | None = None,
        snapshot_timeout_s: float = 15.0,
    ) -> None:
        if timeout_s <= 0 or snapshot_timeout_s <= 0:
            raise ValueError("search timeouts must be positive")
        storage_path = database_path(database.url)
        if snapshots_root is None and storage_path is None:
            raise ValueError("in-memory databases require an explicit snapshots_root")
        if snapshots_root is None:
            assert storage_path is not None
            cache_root = storage_path.parent / "search-snapshots"
        else:
            cache_root = snapshots_root
        self._database = database
        self._rg_binary = rg_binary
        self._sg_binary = sg_binary
        self._timeout_s = timeout_s
        self._snapshots = GitSnapshotStore(cache_root, timeout_s=snapshot_timeout_s)
        self._pinned_commits: ContextVar[dict[tuple[str, str], str] | None] = (
            ContextVar(
                "search_pinned_commits",
                default=None,
            )
        )

    async def list_projects(self) -> ProjectCatalogResult:
        records = await self._project_records()
        return ProjectCatalogResult(projects=tuple(record.target for record in records))

    async def validate_target(self, project_id: str, branch: str) -> None:
        """Resolve and materialize a target before spending a model turn."""
        snapshot = await self._resolve_snapshot(project_id, branch, use_pin=False)
        pins = dict(self._pinned_commits.get() or {})
        pins[(project_id, branch)] = snapshot.commit
        self._pinned_commits.set(pins)

    async def _project_records(self) -> tuple[_ProjectRecord, ...]:
        async with self._database.session() as db_session:
            sessions = (
                await db_session.exec(select(Session).order_by(col(Session.id).desc()))
            ).all()
        paths = await asyncio.to_thread(_known_repository_paths, sessions)
        by_id: dict[str, _ProjectRecord] = {}
        for path in paths:
            try:
                repository = await inspect_repository(path, timeout_s=self._timeout_s)
            except GitUnavailableError as error:
                raise SearchToolUnavailable(str(error)) from error
            except GitReadTimedOut as error:
                raise SearchTimedOut(str(error)) from error
            except GitMetadataTooLarge as error:
                raise SearchOutputTooLarge(str(error)) from error
            except GitReadError:
                # Historical sessions may outlive a deleted or moved repository.
                continue
            project_id = project_id_for(repository.common_dir)
            target = ProjectTarget(
                id=project_id,
                name=repository.root.name,
                repo_path=repository.root,
                branches=tuple(
                    ProjectBranch(
                        name=branch.name,
                        commit=branch.commit,
                        is_head=branch.is_head,
                    )
                    for branch in repository.branches
                    if not _INTERNAL_EXECUTION_BRANCH.fullmatch(branch.name)
                ),
            )
            by_id.setdefault(
                project_id,
                _ProjectRecord(target=target, repository=repository),
            )
        return tuple(
            sorted(
                by_id.values(),
                key=lambda record: (
                    record.target.name.casefold(),
                    record.target.name,
                    record.target.id,
                ),
            )
        )

    async def _snapshot_root(self, project_id: str, branch: str) -> Path:
        snapshot = await self._resolve_snapshot(project_id, branch, use_pin=True)
        return snapshot.root

    async def _resolve_snapshot(
        self,
        project_id: str,
        branch: str,
        *,
        use_pin: bool,
    ) -> GitSnapshot:
        records = await self._project_records()
        record = next(
            (item for item in records if item.target.id == project_id),
            None,
        )
        if record is None:
            raise SearchTargetNotFound(f"no such project: {project_id}")
        pinned = (
            (self._pinned_commits.get() or {}).get((project_id, branch))
            if use_pin
            else None
        )
        if pinned is None:
            selected = next(
                (item for item in record.target.branches if item.name == branch),
                None,
            )
            if selected is None:
                raise SearchTargetNotFound(
                    f"project {project_id} has no local branch named {branch!r}"
                )
            commit = selected.commit
        else:
            commit = pinned
        try:
            snapshot = await self._snapshots.materialize(
                project_id=project_id,
                repository=record.repository.root,
                commit=commit,
            )
        except GitUnavailableError as error:
            raise SearchToolUnavailable(str(error)) from error
        except GitReadTimedOut as error:
            raise SearchTimedOut(str(error)) from error
        except GitSnapshotTooLarge as error:
            raise SearchOutputTooLarge(str(error)) from error
        except (GitSnapshotUnsafe, GitReadError) as error:
            raise SearchTargetNotFound(
                f"cannot materialize branch {branch!r} for project {project_id}"
            ) from error
        return snapshot

    async def search_text(
        self,
        project_id: str,
        branch: str,
        pattern: str,
        *,
        glob: str | None = None,
        case_sensitive: bool = True,
        literal: bool = False,
        limit: int = 100,
    ) -> TextSearchResult:
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"search limit must be between 1 and {MAX_RESULTS}")
        root = await self._snapshot_root(project_id, branch)
        argv = [
            self._rg_binary,
            "--json",
            "--color=never",
            "--max-columns=500",
            "--max-columns-preview",
        ]
        if not case_sensitive:
            argv.append("--ignore-case")
        if literal:
            argv.append("--fixed-strings")
        if glob is not None:
            argv.extend(("--glob", glob))
        argv.extend(("--", pattern, "."))

        matches, truncated, return_code, stderr = await _run_json_stream(
            argv,
            root=root,
            limit=limit,
            timeout_s=self._timeout_s,
            parser=_parse_rg_match,
            operation="text search",
        )

        if not truncated and return_code not in (0, 1):
            detail = stderr.strip() or f"ripgrep exited with status {return_code}"
            raise InvalidSearchPattern(detail)
        return TextSearchResult(matches=matches, truncated=truncated)

    async def search_structural(
        self,
        project_id: str,
        branch: str,
        pattern: str,
        *,
        language: str,
        limit: int = 100,
    ) -> TextSearchResult:
        """Find ast-grep matches with the same citation shape as text search."""
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"search limit must be between 1 and {MAX_RESULTS}")
        root = await self._snapshot_root(project_id, branch)
        argv = [
            self._sg_binary,
            "run",
            "--pattern",
            pattern,
            "--lang",
            language,
            "--json=stream",
            ".",
        ]
        matches, truncated, return_code, stderr = await _run_json_stream(
            argv,
            root=root,
            limit=limit,
            timeout_s=self._timeout_s,
            parser=_parse_sg_match,
            operation="structural search",
        )
        if not truncated and return_code != 0:
            detail = stderr.strip() or f"ast-grep exited with status {return_code}"
            raise InvalidStructuralPattern(detail)
        return TextSearchResult(matches=matches, truncated=truncated)

    async def read_file(
        self,
        project_id: str,
        branch: str,
        path: str,
        *,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> FileReadResult:
        if start_line < 1:
            raise ValueError("start_line must be positive")
        final_line = start_line + MAX_FILE_LINES - 1 if end_line is None else end_line
        if final_line < start_line:
            raise ValueError("end_line must not precede start_line")
        if final_line - start_line + 1 > MAX_FILE_LINES:
            raise ValueError(f"file reads are limited to {MAX_FILE_LINES} lines")
        root = await self._snapshot_root(project_id, branch)
        return await asyncio.to_thread(
            _read_file_sync, root, path, start_line, final_line
        )

    async def list_directory(
        self,
        project_id: str,
        branch: str,
        path: str = ".",
        *,
        limit: int = 200,
    ) -> DirectoryListResult:
        if not 1 <= limit <= MAX_DIRECTORY_ENTRIES:
            raise ValueError(
                f"directory limit must be between 1 and {MAX_DIRECTORY_ENTRIES}"
            )
        root = await self._snapshot_root(project_id, branch)
        return await asyncio.to_thread(_list_directory_sync, root, path, limit)


def _known_repository_paths(sessions: Sequence[Session]) -> tuple[Path, ...]:
    paths: dict[Path, None] = {}
    for session in sessions:
        try:
            resolved = session.repo_path.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        paths.setdefault(resolved, None)
    return tuple(paths)


def _resolve_relative(root: Path, value: str) -> tuple[Path, str]:
    relative = PurePosixPath(value)
    if relative.is_absolute():
        raise SearchPathError("search paths must be repository-relative")
    if ".." in relative.parts:
        raise SearchPathError("search paths must not contain parent traversal")
    try:
        candidate = (root / Path(*relative.parts)).resolve(strict=True)
    except FileNotFoundError as error:
        raise SearchTargetNotFound(f"no such repository path: {value}") from error
    if candidate != root and root not in candidate.parents:
        raise SearchPathError(f"repository path escapes the branch snapshot: {value}")
    display = "." if candidate == root else candidate.relative_to(root).as_posix()
    return candidate, display


def _read_file_sync(
    root: Path, value: str, start_line: int, end_line: int
) -> FileReadResult:
    path, display = _resolve_relative(root, value)
    if not path.is_file():
        raise SearchTargetNotFound(f"repository path is not a file: {value}")
    lines: list[FileLine] = []
    scanned = 0
    rendered_bytes = 0
    truncated = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for number, text in enumerate(handle, start=1):
            scanned += len(text.encode("utf-8"))
            if scanned > MAX_SCAN_BYTES:
                truncated = True
                break
            if number < start_line:
                continue
            if number > end_line:
                break
            rendered = text.rstrip("\r\n")
            line_bytes = len(rendered.encode("utf-8"))
            if rendered_bytes + line_bytes > MAX_FILE_BYTES:
                truncated = True
                break
            lines.append(FileLine(line=number, text=rendered))
            rendered_bytes += line_bytes
    result_lines = tuple(lines)
    return FileReadResult(
        path=display,
        lines=result_lines,
        truncated=truncated,
        content_hash=file_line_hash(result_lines),
    )


def file_line_hash(lines: Sequence[FileLine]) -> str:
    """Fingerprint an exact numbered line range for stale-citation detection."""
    digest = hashlib.sha256()
    for line in lines:
        encoded = line.text.encode("utf-8")
        digest.update(line.line.to_bytes(8, byteorder="big", signed=False))
        digest.update(len(encoded).to_bytes(8, byteorder="big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _list_directory_sync(root: Path, value: str, limit: int) -> DirectoryListResult:
    path, display = _resolve_relative(root, value)
    if not path.is_dir():
        raise SearchTargetNotFound(f"repository path is not a directory: {value}")
    children = sorted(path.iterdir(), key=lambda item: item.name.casefold())
    truncated = len(children) > limit
    entries: list[DirectoryEntry] = []
    for child in children[:limit]:
        resolved = child.resolve(strict=False)
        if resolved != root and root not in resolved.parents:
            # Do not advertise a symlink the follow-up read is forbidden to open.
            continue
        kind = "directory" if child.is_dir() else "file" if child.is_file() else "other"
        entries.append(
            DirectoryEntry(path=child.relative_to(root).as_posix(), kind=kind)
        )
    return DirectoryListResult(
        path=display, entries=tuple(entries), truncated=truncated
    )


def _parse_rg_match(raw: bytes) -> TextMatch | None:
    try:
        message: Any = json.loads(raw)
        if message.get("type") != "match":
            return None
        data = message["data"]
        path = data["path"]["text"]
        preview = data["lines"]["text"].rstrip("\r\n")
        submatches = data["submatches"]
        if submatches:
            byte_offset = int(submatches[0]["start"])
            column = (
                len(
                    preview.encode("utf-8")[:byte_offset].decode(
                        "utf-8", errors="ignore"
                    )
                )
                + 1
            )
        else:
            column = 1
        return TextMatch(
            path=PurePosixPath(path).as_posix().removeprefix("./"),
            line=int(data["line_number"]),
            column=column,
            preview=preview,
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, IndexError):
        return None


def _parse_sg_match(raw: bytes) -> TextMatch | None:
    try:
        message: Any = json.loads(raw)
        path = _safe_result_path(message["file"])
        if path is None:
            return None
        start = message["range"]["start"]
        preview = str(message.get("lines", message["text"])).rstrip("\r\n")
        return TextMatch(
            path=path,
            line=int(start["line"]) + 1,
            column=int(start["column"]) + 1,
            preview=preview[:MAX_PREVIEW_CHARS],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _safe_result_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix().removeprefix("./")


async def _run_json_stream[T](
    argv: list[str],
    *,
    root: Path,
    limit: int,
    timeout_s: float,
    parser: Callable[[bytes], T | None],
    operation: str,
) -> tuple[tuple[T, ...], bool, int, str]:
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1_048_576,
        )
    except FileNotFoundError as error:
        raise SearchToolUnavailable(
            f"{operation} executable is unavailable: {argv[0]}"
        ) from error

    assert process.stdout is not None
    assert process.stderr is not None
    stderr_task = asyncio.create_task(_read_stderr(process.stderr))
    matches: list[T] = []
    truncated = False
    try:
        async with asyncio.timeout(timeout_s):
            while line := await process.stdout.readline():
                match = parser(line)
                if match is None:
                    continue
                matches.append(match)
                if len(matches) > limit:
                    matches.pop()
                    truncated = True
                    with suppress(ProcessLookupError):
                        process.terminate()
                    break
            return_code = await process.wait()
    except TimeoutError as error:
        await _stop(process)
        raise SearchTimedOut(f"{operation} exceeded {timeout_s:g} seconds") from error
    except ValueError as error:
        await _stop(process)
        raise SearchOutputTooLarge(
            f"{operation} emitted a result larger than the stream limit"
        ) from error
    except BaseException:
        await _stop(process)
        raise
    finally:
        stderr = await stderr_task
    return tuple(matches), truncated, return_code, stderr


async def _read_stderr(stream: asyncio.StreamReader) -> str:
    captured = bytearray()
    while chunk := await stream.read(4_096):
        remaining = MAX_STDERR_BYTES - len(captured)
        if remaining > 0:
            captured.extend(chunk[:remaining])
    return bytes(captured).decode("utf-8", errors="replace")


async def _stop(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        process.kill()
    await process.wait()


__all__ = [
    "CodeSearchService",
    "DirectoryEntry",
    "DirectoryListResult",
    "FileLine",
    "FileReadResult",
    "InvalidSearchPattern",
    "InvalidStructuralPattern",
    "ProjectBranch",
    "ProjectCatalogResult",
    "ProjectTarget",
    "SearchError",
    "SearchOutputTooLarge",
    "SearchPathError",
    "SearchTargetNotFound",
    "SearchTimedOut",
    "SearchToolUnavailable",
    "TextMatch",
    "TextSearchResult",
    "file_line_hash",
]
