"""Incremental code chunks ranked by sqlite-vec as a last-resort search tool."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol, cast

import sqlite_vec as _sqlite_vec  # type: ignore[import-untyped]
from sqlmodel import col, delete, select

from app.models.ids import SessionId
from app.models.tables import CodeSymbol, SemanticChunk, SemanticSource, Session
from app.search.symbols import TreeSitterSymbolExtractor
from app.search.tools import SearchTargetNotFound
from app.storage.db import Database, database_path

EMBEDDING_DIMENSIONS = 256
MAX_CHUNK_LINES = 80
CHUNK_OVERLAP_LINES = 10
MAX_SEMANTIC_RESULTS = 50
MAX_SEMANTIC_SOURCE_BYTES = 2 * 1_024 * 1_024
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class _SQLiteVec(Protocol):
    def load(self, connection: sqlite3.Connection) -> None: ...


sqlite_vec = cast(_SQLiteVec, _sqlite_vec)


def _serialize(vector: tuple[float, ...]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    path: str
    line: int
    end_line: int
    preview: str
    distance: float


@dataclass(frozen=True, slots=True)
class SemanticSearchResult:
    matches: tuple[SemanticMatch, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class _Snapshot:
    path: str
    source_hash: str
    text: str


@dataclass(frozen=True, slots=True)
class _PreparedChunk:
    start_line: int
    end_line: int
    text: str
    embedding: bytes


class SemanticIndexService:
    """Persist bounded chunks and rank them with sqlite-vec off the event loop."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._languages = TreeSitterSymbolExtractor()
        path = database_path(database.url)
        if path is None:
            raise ValueError("semantic index requires a file-backed SQLite database")
        self._database_path = path

    async def sync_session(self, session_id: SessionId, root: Path) -> int:
        paths = await asyncio.to_thread(_discover_paths, root, self._languages)
        async with self._database.session() as db_session:
            indexed = {
                row.path
                for row in (
                    await db_session.exec(
                        select(SemanticSource).where(
                            col(SemanticSource.session_id) == session_id
                        )
                    )
                ).all()
            }
        changed = 0
        for path in paths:
            changed += await self.index_path(session_id, root, path)
        for path in indexed - set(paths):
            changed += await self.remove_path(session_id, path)
        return changed

    async def index_path(self, session_id: SessionId, root: Path, path: str) -> int:
        snapshot = await asyncio.to_thread(_read_source, root, path, self._languages)
        if snapshot is None:
            return await self.remove_path(session_id, path)
        async with self._database.session() as db_session:
            existing = await db_session.get(SemanticSource, (session_id, path))
            if existing is not None and existing.source_hash == snapshot.source_hash:
                return 0
            definitions = tuple(
                (
                    await db_session.exec(
                        select(CodeSymbol.start_line)
                        .where(
                            col(CodeSymbol.session_id) == session_id,
                            col(CodeSymbol.path) == path,
                            col(CodeSymbol.role) == "definition",
                        )
                        .order_by(col(CodeSymbol.start_line))
                    )
                ).all()
            )
        chunks = await asyncio.to_thread(_prepare_chunks, snapshot.text, definitions)
        async with self._database.session() as db_session:
            await db_session.exec(
                delete(SemanticChunk).where(
                    col(SemanticChunk.session_id) == session_id,
                    col(SemanticChunk.path) == path,
                )
            )
            source = await db_session.get(SemanticSource, (session_id, path))
            if source is None:
                source = SemanticSource(
                    session_id=session_id,
                    path=path,
                    source_hash=snapshot.source_hash,
                )
            else:
                source.source_hash = snapshot.source_hash
            db_session.add(source)
            await db_session.flush()
            for chunk in chunks:
                db_session.add(
                    SemanticChunk(
                        session_id=session_id,
                        path=path,
                        source_hash=snapshot.source_hash,
                        start_line=chunk.start_line,
                        end_line=chunk.end_line,
                        text=chunk.text,
                        embedding=chunk.embedding,
                    )
                )
            await db_session.commit()
        return 1

    async def remove_path(self, session_id: SessionId, path: str) -> int:
        async with self._database.session() as db_session:
            source = await db_session.get(SemanticSource, (session_id, path))
            if source is None:
                return 0
            await db_session.delete(source)
            await db_session.commit()
        return 1

    async def search(
        self, session_id: SessionId, query: str, *, limit: int = 10
    ) -> SemanticSearchResult:
        if not query.strip():
            raise ValueError("semantic query must not be empty")
        if not 1 <= limit <= MAX_SEMANTIC_RESULTS:
            raise ValueError(
                f"semantic limit must be between 1 and {MAX_SEMANTIC_RESULTS}"
            )
        async with self._database.session() as db_session:
            if await db_session.get(Session, session_id) is None:
                raise SearchTargetNotFound(f"no such session: {session_id}")
        ranked = await asyncio.to_thread(
            _rank,
            self._database_path,
            session_id,
            _embed(query),
            limit + 1,
        )
        return SemanticSearchResult(
            matches=ranked[:limit], truncated=len(ranked) > limit
        )


def _discover_paths(
    root: Path, languages: TreeSitterSymbolExtractor
) -> tuple[str, ...]:
    ignored = {".git", ".venv", "build", "dist", "node_modules"}
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and not any(part in ignored for part in path.relative_to(root).parts)
            and languages.language_for_path(path.relative_to(root).as_posix())
        )
    )


def _read_source(
    root: Path, display: str, languages: TreeSitterSymbolExtractor
) -> _Snapshot | None:
    if languages.language_for_path(display) is None:
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
    source = path.read_bytes()
    if len(source) > MAX_SEMANTIC_SOURCE_BYTES:
        return None
    return _Snapshot(
        path=display,
        source_hash=hashlib.sha256(source).hexdigest(),
        text=source.decode("utf-8", errors="replace"),
    )


def _prepare_chunks(
    text: str, definitions: tuple[int, ...]
) -> tuple[_PreparedChunk, ...]:
    lines = text.splitlines()
    if not lines:
        return ()
    anchors = sorted({1, *(line for line in definitions if 1 <= line <= len(lines))})
    ranges: list[tuple[int, int]] = []
    for index, start in enumerate(anchors):
        boundary = anchors[index + 1] - 1 if index + 1 < len(anchors) else len(lines)
        cursor = start
        while cursor <= boundary:
            end = min(boundary, cursor + MAX_CHUNK_LINES - 1)
            ranges.append((cursor, end))
            if end == boundary:
                break
            cursor = end - CHUNK_OVERLAP_LINES + 1
    chunks = []
    for start, end in ranges:
        content = "\n".join(lines[start - 1 : end])
        if not content.strip():
            continue
        chunks.append(
            _PreparedChunk(
                start_line=start,
                end_line=end,
                text=content,
                embedding=_serialize(_embed(content)),
            )
        )
    return tuple(chunks)


def _embed(text: str) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    for raw in _TOKEN.findall(text):
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw).replace("_", " ")
        for token in {raw.casefold(), *(part.casefold() for part in expanded.split())}:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            values[bucket] += -1.0 if digest[4] & 1 else 1.0
    norm = math.sqrt(sum(value * value for value in values))
    return tuple(value / norm for value in values) if norm else tuple(values)


def _rank(
    database: Path, session_id: SessionId, query: tuple[float, ...], limit: int
) -> tuple[SemanticMatch, ...]:
    if not any(query):
        return ()
    connection = sqlite3.connect(database)
    try:
        connection.enable_load_extension(True)
        sqlite_vec.load(connection)
        found = connection.execute(
            "SELECT path, start_line, end_line, text, "
            "vec_distance_cosine(embedding, ?) AS distance "
            "FROM semantic_chunk WHERE session_id = ? "
            "ORDER BY distance, path, start_line LIMIT ?",
            (_serialize(query), session_id, limit),
        ).fetchall()
        return tuple(
            SemanticMatch(
                path=str(path),
                line=int(start_line),
                end_line=int(end_line),
                preview=str(text).splitlines()[0][:500],
                distance=float(distance),
            )
            for path, start_line, end_line, text, distance in found
        )
    finally:
        connection.close()


__all__ = ["SemanticIndexService", "SemanticMatch", "SemanticSearchResult"]
