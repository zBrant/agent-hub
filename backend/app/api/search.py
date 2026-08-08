"""Typed HTTP boundary for the bounded code-search tools."""

from __future__ import annotations

from typing import Annotated, NoReturn, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.search.tools import (
    CodeSearchService,
    DirectoryListResult,
    FileReadResult,
    InvalidSearchPattern,
    InvalidStructuralPattern,
    SearchOutputTooLarge,
    SearchPathError,
    SearchTargetNotFound,
    SearchTimedOut,
    SearchToolUnavailable,
    TextSearchResult,
)

router = APIRouter(prefix="/api/search", tags=["search"])


class TextMatchResponse(BaseModel):
    path: str
    line: int
    column: int
    preview: str


class TextSearchResponse(BaseModel):
    matches: tuple[TextMatchResponse, ...]
    truncated: bool

    @classmethod
    def from_result(cls, result: TextSearchResult) -> TextSearchResponse:
        return cls(
            matches=tuple(
                TextMatchResponse(
                    path=match.path,
                    line=match.line,
                    column=match.column,
                    preview=match.preview,
                )
                for match in result.matches
            ),
            truncated=result.truncated,
        )


class FileLineResponse(BaseModel):
    line: int
    text: str


class FileReadResponse(BaseModel):
    path: str
    lines: tuple[FileLineResponse, ...]
    truncated: bool

    @classmethod
    def from_result(cls, result: FileReadResult) -> FileReadResponse:
        return cls(
            path=result.path,
            lines=tuple(
                FileLineResponse(line=line.line, text=line.text)
                for line in result.lines
            ),
            truncated=result.truncated,
        )


class DirectoryEntryResponse(BaseModel):
    path: str
    kind: str


class DirectoryListResponse(BaseModel):
    path: str
    entries: tuple[DirectoryEntryResponse, ...]
    truncated: bool

    @classmethod
    def from_result(cls, result: DirectoryListResult) -> DirectoryListResponse:
        return cls(
            path=result.path,
            entries=tuple(
                DirectoryEntryResponse(path=entry.path, kind=entry.kind)
                for entry in result.entries
            ),
            truncated=result.truncated,
        )


def _service(request: Request) -> CodeSearchService:
    return cast(CodeSearchService, request.app.state.search)


def _raise_http(error: Exception) -> NoReturn:
    if isinstance(error, SearchTargetNotFound):
        raise HTTPException(status_code=404, detail=str(error)) from error
    if isinstance(
        error, (SearchPathError, InvalidSearchPattern, InvalidStructuralPattern)
    ):
        raise HTTPException(status_code=400, detail=str(error)) from error
    if isinstance(error, SearchOutputTooLarge):
        raise HTTPException(status_code=413, detail=str(error)) from error
    if isinstance(error, SearchToolUnavailable):
        raise HTTPException(status_code=503, detail=str(error)) from error
    if isinstance(error, SearchTimedOut):
        raise HTTPException(status_code=504, detail=str(error)) from error
    raise error


@router.get("/text", response_model=TextSearchResponse)
async def search_text(
    request: Request,
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    pattern: Annotated[str, Query(min_length=1, max_length=1_000)],
    glob: Annotated[str | None, Query(max_length=256)] = None,
    case_sensitive: bool = True,
    literal: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> TextSearchResponse:
    try:
        result = await _service(request).search_text(
            session_id,
            pattern,
            glob=glob,
            case_sensitive=case_sensitive,
            literal=literal,
            limit=limit,
        )
    except (
        SearchTargetNotFound,
        SearchPathError,
        InvalidSearchPattern,
        SearchOutputTooLarge,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        _raise_http(error)
    return TextSearchResponse.from_result(result)


@router.get("/structural", response_model=TextSearchResponse)
async def search_structural(
    request: Request,
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    pattern: Annotated[str, Query(min_length=1, max_length=1_000)],
    language: Annotated[str, Query(min_length=1, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> TextSearchResponse:
    try:
        result = await _service(request).search_structural(
            session_id, pattern, language=language, limit=limit
        )
    except (
        SearchTargetNotFound,
        SearchPathError,
        InvalidStructuralPattern,
        SearchOutputTooLarge,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        _raise_http(error)
    return TextSearchResponse.from_result(result)


@router.get("/file", response_model=FileReadResponse)
async def read_file(
    request: Request,
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    path: Annotated[str, Query(min_length=1, max_length=1_000)],
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileReadResponse:
    try:
        result = await _service(request).read_file(
            session_id, path, start_line=start_line, end_line=end_line
        )
    except (
        ValueError,
        SearchTargetNotFound,
        SearchPathError,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        if isinstance(error, ValueError):
            raise HTTPException(status_code=400, detail=str(error)) from error
        _raise_http(error)
    return FileReadResponse.from_result(result)


@router.get("/directory", response_model=DirectoryListResponse)
async def list_directory(
    request: Request,
    session_id: Annotated[str, Query(min_length=1, max_length=128)],
    path: Annotated[str, Query(min_length=1, max_length=1_000)] = ".",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> DirectoryListResponse:
    try:
        result = await _service(request).list_directory(session_id, path, limit=limit)
    except (
        SearchTargetNotFound,
        SearchPathError,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        _raise_http(error)
    return DirectoryListResponse.from_result(result)


__all__ = ["router"]
