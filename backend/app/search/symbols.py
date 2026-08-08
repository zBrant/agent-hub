"""Incremental Tree-sitter tags index for session integration worktrees."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Protocol

import structlog
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from sqlmodel import col, delete, select
from tree_sitter import Language, Parser, Query, QueryCursor
from watchfiles import DefaultFilter, awatch

from app.models.ids import SessionId
from app.models.tables import CodeSymbol, Session, SymbolSource
from app.search.tools import SearchTargetNotFound
from app.storage.db import Database

MAX_SYMBOL_RESULTS = 200
MAX_SYMBOL_SOURCE_BYTES = 2 * 1_024 * 1_024
DISCOVERY_INTERVAL_S = 1.0
_IGNORED_PARTS = frozenset(
    {".git", ".next", ".venv", "__pycache__", "build", "dist", "node_modules"}
)

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class ExtractedSymbol:
    name: str
    kind: str
    role: str
    start_line: int
    start_column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class SymbolMatch:
    path: str
    language: str
    name: str
    kind: str
    role: str
    line: int
    column: int
    end_line: int
    end_column: int


@dataclass(frozen=True, slots=True)
class SymbolSearchResult:
    matches: tuple[SymbolMatch, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    path: str
    language: str
    source: bytes
    source_hash: str


class SymbolExtractor(Protocol):
    def language_for_path(self, path: str) -> str | None: ...

    def extract(self, language: str, source: bytes) -> tuple[ExtractedSymbol, ...]: ...


class TreeSitterSymbolExtractor:
    """Offline parsers for the Python and web languages used by AgentHub."""

    def language_for_path(self, path: str) -> str | None:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix == ".py":
            return "python"
        if suffix in {".js", ".jsx", ".mjs", ".cjs"}:
            return "javascript"
        if suffix in {".ts", ".mts", ".cts"}:
            return "typescript"
        if suffix == ".tsx":
            return "tsx"
        return None

    def extract(self, language: str, source: bytes) -> tuple[ExtractedSymbol, ...]:
        grammar, query = _grammar_and_query(language)
        tree = Parser(grammar).parse(source)
        matches = QueryCursor(query).matches(tree.root_node)
        found: list[ExtractedSymbol] = []
        seen: set[tuple[str, str, str, int, int, int, int]] = set()
        for _, captures in matches:
            names = captures.get("name", [])
            if not names:
                continue
            name_node = names[0]
            name = source[name_node.start_byte : name_node.end_byte].decode(
                "utf-8", errors="replace"
            )
            for capture in captures:
                if capture.startswith("definition."):
                    role = "definition"
                elif capture.startswith("reference."):
                    role = "reference"
                else:
                    continue
                kind = capture.split(".", 1)[1]
                key = (
                    name,
                    kind,
                    role,
                    name_node.start_point.row,
                    name_node.start_point.column,
                    name_node.end_point.row,
                    name_node.end_point.column,
                )
                if key in seen:
                    continue
                seen.add(key)
                found.append(
                    ExtractedSymbol(
                        name=name,
                        kind=kind,
                        role=role,
                        start_line=name_node.start_point.row + 1,
                        start_column=name_node.start_point.column + 1,
                        end_line=name_node.end_point.row + 1,
                        end_column=name_node.end_point.column + 1,
                    )
                )
        return tuple(found)


@lru_cache(maxsize=4)
def _grammar_and_query(language: str) -> tuple[Language, Query]:
    if language == "python":
        grammar = Language(tree_sitter_python.language())
        query_name = "python"
    elif language == "javascript":
        grammar = Language(tree_sitter_javascript.language())
        query_name = "javascript"
    elif language == "typescript":
        grammar = Language(tree_sitter_typescript.language_typescript())
        query_name = "typescript"
    elif language == "tsx":
        grammar = Language(tree_sitter_typescript.language_tsx())
        query_name = "typescript"
    else:  # pragma: no cover - guarded by language_for_path
        raise ValueError(f"unsupported symbol language: {language}")
    query_path = Path(__file__).parent / "queries" / query_name / "tags.scm"
    return grammar, Query(grammar, query_path.read_text(encoding="utf-8"))


class SymbolIndexService:
    """Persist and query per-file symbols without indexing on a request path."""

    def __init__(
        self, database: Database, *, extractor: SymbolExtractor | None = None
    ) -> None:
        self._database = database
        self._extractor = extractor or TreeSitterSymbolExtractor()

    async def sync_session(self, session_id: SessionId, root: Path) -> int:
        paths = await asyncio.to_thread(_discover_source_paths, root, self._extractor)
        async with self._database.session() as db_session:
            indexed = {
                source.path
                for source in (
                    await db_session.exec(
                        select(SymbolSource).where(
                            col(SymbolSource.session_id) == session_id
                        )
                    )
                ).all()
            }
        changed = 0
        for path in paths:
            changed += await self.index_path(session_id, root, path)
        for deleted_path in indexed - set(paths):
            changed += await self.remove_path(session_id, deleted_path)
        return changed

    async def index_path(self, session_id: SessionId, root: Path, path: str) -> int:
        snapshot = await asyncio.to_thread(_read_source, root, path, self._extractor)
        if snapshot is None:
            return await self.remove_path(session_id, path)
        async with self._database.session() as db_session:
            existing = await db_session.get(SymbolSource, (session_id, path))
            if existing is not None and existing.source_hash == snapshot.source_hash:
                return 0

        extracted = await asyncio.to_thread(
            self._extractor.extract, snapshot.language, snapshot.source
        )
        async with self._database.session() as db_session:
            await db_session.exec(
                delete(CodeSymbol).where(
                    col(CodeSymbol.session_id) == session_id,
                    col(CodeSymbol.path) == path,
                )
            )
            source_row = await db_session.get(SymbolSource, (session_id, path))
            if source_row is None:
                source_row = SymbolSource(
                    session_id=session_id,
                    path=path,
                    source_hash=snapshot.source_hash,
                    language=snapshot.language,
                )
                db_session.add(source_row)
            else:
                source_row.source_hash = snapshot.source_hash
                source_row.language = snapshot.language
                db_session.add(source_row)
            # The ORM models intentionally have no relationship collections.
            # Flush the composite parent explicitly so SQLite can enforce the
            # source FK while the symbol batch is inserted below.
            await db_session.flush()
            for symbol in extracted:
                db_session.add(
                    CodeSymbol(
                        session_id=session_id,
                        path=path,
                        source_hash=snapshot.source_hash,
                        language=snapshot.language,
                        name=symbol.name,
                        kind=symbol.kind,
                        role=symbol.role,
                        start_line=symbol.start_line,
                        start_column=symbol.start_column,
                        end_line=symbol.end_line,
                        end_column=symbol.end_column,
                    )
                )
            await db_session.commit()
        return 1

    async def remove_path(self, session_id: SessionId, path: str) -> int:
        async with self._database.session() as db_session:
            source = await db_session.get(SymbolSource, (session_id, path))
            if source is None:
                return 0
            await db_session.delete(source)
            await db_session.commit()
        return 1

    async def find_symbol(
        self,
        session_id: SessionId,
        name: str,
        *,
        role: str | None = "definition",
        kind: str | None = None,
        limit: int = 100,
    ) -> SymbolSearchResult:
        if not 1 <= limit <= MAX_SYMBOL_RESULTS:
            raise ValueError(f"symbol limit must be between 1 and {MAX_SYMBOL_RESULTS}")
        statement = select(CodeSymbol).where(
            col(CodeSymbol.session_id) == session_id,
            col(CodeSymbol.name) == name,
        )
        if role is not None:
            statement = statement.where(col(CodeSymbol.role) == role)
        if kind is not None:
            statement = statement.where(col(CodeSymbol.kind) == kind)
        async with self._database.session() as db_session:
            if await db_session.get(Session, session_id) is None:
                raise SearchTargetNotFound(f"no such session: {session_id}")
            rows = tuple(
                (
                    await db_session.exec(
                        statement.order_by(
                            col(CodeSymbol.path),
                            col(CodeSymbol.start_line),
                            col(CodeSymbol.start_column),
                        ).limit(limit + 1)
                    )
                ).all()
            )
        truncated = len(rows) > limit
        return SymbolSearchResult(
            matches=tuple(_match(row) for row in rows[:limit]), truncated=truncated
        )

    async def find_references(
        self, session_id: SessionId, name: str, *, limit: int = 100
    ) -> SymbolSearchResult:
        return await self.find_symbol(session_id, name, role="reference", limit=limit)


def _match(row: CodeSymbol) -> SymbolMatch:
    return SymbolMatch(
        path=row.path,
        language=row.language,
        name=row.name,
        kind=row.kind,
        role=row.role,
        line=row.start_line,
        column=row.start_column,
        end_line=row.end_line,
        end_column=row.end_column,
    )


def _discover_source_paths(root: Path, extractor: SymbolExtractor) -> tuple[str, ...]:
    paths: list[str] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        display = relative.as_posix()
        if (
            path.is_file()
            and not path.is_symlink()
            and extractor.language_for_path(display)
        ):
            paths.append(display)
    return tuple(sorted(paths))


def _read_source(
    root: Path, display: str, extractor: SymbolExtractor
) -> _SourceSnapshot | None:
    language = extractor.language_for_path(display)
    if language is None:
        return None
    relative = PurePosixPath(display)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    try:
        resolved_root = root.resolve(strict=True)
        path = (resolved_root / Path(*relative.parts)).resolve(strict=True)
    except FileNotFoundError:
        return None
    if resolved_root not in path.parents or not path.is_file():
        return None
    with path.open("rb") as handle:
        source = handle.read(MAX_SYMBOL_SOURCE_BYTES + 1)
    if len(source) > MAX_SYMBOL_SOURCE_BYTES:
        return None
    return _SourceSnapshot(
        path=display,
        language=language,
        source=source,
        source_hash=hashlib.sha256(source).hexdigest(),
    )


class SymbolIndexManager:
    """Lifespan-owned discovery and watch loop for every persisted session."""

    def __init__(
        self,
        database: Database,
        service: SymbolIndexService,
        *,
        discovery_interval_s: float = DISCOVERY_INTERVAL_S,
    ) -> None:
        self._database = database
        self._service = service
        self._discovery_interval_s = discovery_interval_s
        self._coordinator: asyncio.Task[None] | None = None
        self._watchers: dict[SessionId, asyncio.Task[None]] = {}

    def start(self) -> None:
        if self._coordinator is not None:
            raise RuntimeError("symbol index manager is already started")
        self._coordinator = asyncio.create_task(
            self._discover_loop(), name="symbol-index-discovery"
        )

    async def close(self) -> None:
        tasks = [*self._watchers.values()]
        if self._coordinator is not None:
            tasks.append(self._coordinator)
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as error:
                log.error("search.symbol_task_close_failed", error=str(error))
        self._watchers.clear()
        self._coordinator = None

    async def _discover_loop(self) -> None:
        while True:
            for session_id, task in tuple(self._watchers.items()):
                if task.done():
                    self._watchers.pop(session_id)
                    error = None if task.cancelled() else task.exception()
                    if error is not None:
                        log.error(
                            "search.symbol_watcher_failed",
                            session_id=session_id,
                            error=str(error),
                        )
            async with self._database.session() as db_session:
                sessions: Sequence[Session] = (
                    await db_session.exec(select(Session).order_by(col(Session.id)))
                ).all()
            for session in sessions:
                if session.id in self._watchers:
                    continue
                root = session.workspace_root / "integration"
                if await asyncio.to_thread(root.is_dir):
                    self._watchers[session.id] = asyncio.create_task(
                        self._watch(session.id, root),
                        name=f"symbol-index-{session.id}",
                    )
            await asyncio.sleep(self._discovery_interval_s)

    async def _watch(self, session_id: SessionId, root: Path) -> None:
        await self._service.sync_session(session_id, root)
        async for changes in awatch(root, watch_filter=DefaultFilter()):
            paths = {
                Path(raw_path).relative_to(root).as_posix()
                for _, raw_path in changes
                if Path(raw_path) != root and Path(raw_path).is_relative_to(root)
            }
            for path in sorted(paths):
                await self._service.index_path(session_id, root, path)


__all__ = [
    "ExtractedSymbol",
    "SymbolIndexManager",
    "SymbolIndexService",
    "SymbolMatch",
    "SymbolSearchResult",
    "TreeSitterSymbolExtractor",
]
