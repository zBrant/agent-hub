"""Bounded lexical and filesystem tools for one integration worktree."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from app.models.ids import SessionId
from app.models.tables import Session
from app.storage.db import Database

MAX_RESULTS = 200
MAX_STDERR_BYTES = 8_192
MAX_FILE_LINES = 400
MAX_FILE_BYTES = 128 * 1_024
MAX_SCAN_BYTES = 2 * 1_024 * 1_024
MAX_DIRECTORY_ENTRIES = 500
DEFAULT_TIMEOUT_S = 5.0


class SearchError(Exception):
    """Base error for a bounded code-search operation."""


class SearchTargetNotFound(SearchError):
    """The session, integration worktree, or requested path does not exist."""


class SearchPathError(SearchError):
    """A requested path is absolute or escapes the integration worktree."""


class SearchToolUnavailable(SearchError):
    """A required local search executable is not installed."""


class InvalidSearchPattern(SearchError):
    """ripgrep rejected the supplied regular expression or glob."""


class SearchTimedOut(SearchError):
    """A search exceeded its fixed wall-clock budget."""


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


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: str
    kind: str


@dataclass(frozen=True, slots=True)
class DirectoryListResult:
    path: str
    entries: tuple[DirectoryEntry, ...]
    truncated: bool


class CodeSearchService:
    """Resolve a session target, then run bounded read-only search tools."""

    def __init__(
        self,
        database: Database,
        *,
        rg_binary: str = "rg",
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        if timeout_s <= 0:
            raise ValueError("search timeout must be positive")
        self._database = database
        self._rg_binary = rg_binary
        self._timeout_s = timeout_s

    async def search_text(
        self,
        session_id: SessionId,
        pattern: str,
        *,
        glob: str | None = None,
        case_sensitive: bool = True,
        literal: bool = False,
        limit: int = 100,
    ) -> TextSearchResult:
        if not 1 <= limit <= MAX_RESULTS:
            raise ValueError(f"search limit must be between 1 and {MAX_RESULTS}")
        root = await self._integration_root(session_id)
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
                f"text search executable is unavailable: {self._rg_binary}"
            ) from error

        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(_read_stderr(process.stderr))
        matches: list[TextMatch] = []
        truncated = False
        try:
            async with asyncio.timeout(self._timeout_s):
                while line := await process.stdout.readline():
                    match = _parse_rg_match(line)
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
            raise SearchTimedOut(
                f"text search exceeded {self._timeout_s:g} seconds"
            ) from error
        except BaseException:
            await _stop(process)
            raise
        finally:
            stderr = await stderr_task

        if not truncated and return_code not in (0, 1):
            detail = stderr.strip() or f"ripgrep exited with status {return_code}"
            raise InvalidSearchPattern(detail)
        return TextSearchResult(matches=tuple(matches), truncated=truncated)

    async def read_file(
        self,
        session_id: SessionId,
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
        root = await self._integration_root(session_id)
        return await asyncio.to_thread(
            _read_file_sync, root, path, start_line, final_line
        )

    async def list_directory(
        self, session_id: SessionId, path: str = ".", *, limit: int = 200
    ) -> DirectoryListResult:
        if not 1 <= limit <= MAX_DIRECTORY_ENTRIES:
            raise ValueError(
                f"directory limit must be between 1 and {MAX_DIRECTORY_ENTRIES}"
            )
        root = await self._integration_root(session_id)
        return await asyncio.to_thread(_list_directory_sync, root, path, limit)

    async def _integration_root(self, session_id: SessionId) -> Path:
        async with self._database.session() as db_session:
            session = await db_session.get(Session, session_id)
        if session is None:
            raise SearchTargetNotFound(f"no such session: {session_id}")
        return await asyncio.to_thread(
            _resolve_integration_root, session.workspace_root
        )


def _resolve_integration_root(workspace_root: Path) -> Path:
    try:
        resolved_workspace = workspace_root.resolve(strict=True)
        resolved = (resolved_workspace / "integration").resolve(strict=True)
    except FileNotFoundError as error:
        raise SearchTargetNotFound(
            f"integration worktree does not exist: {workspace_root / 'integration'}"
        ) from error
    if resolved_workspace not in resolved.parents:
        integration = workspace_root / "integration"
        raise SearchPathError(
            f"integration worktree escapes its workspace: {integration}"
        )
    if not resolved.is_dir():
        raise SearchTargetNotFound(
            f"integration worktree is not a directory: {resolved}"
        )
    return resolved


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
        raise SearchPathError(
            f"repository path escapes the integration worktree: {value}"
        )
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
    return FileReadResult(path=display, lines=tuple(lines), truncated=truncated)


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
    "SearchError",
    "SearchPathError",
    "SearchTargetNotFound",
    "SearchTimedOut",
    "SearchToolUnavailable",
    "TextMatch",
    "TextSearchResult",
]
