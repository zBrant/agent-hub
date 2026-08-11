"""Typed HTTP boundary for the bounded code-search tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, NoReturn, cast

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, StringConstraints

from app.search.agent import SearchAgent, SearchAnswer
from app.search.tools import (
    CodeSearchService,
    DirectoryListResult,
    FileReadResult,
    InvalidSearchPattern,
    InvalidStructuralPattern,
    ProjectCatalogResult,
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


class AgentSearchRequest(BaseModel):
    project_id: str = Field(min_length=1, max_length=128)
    branch: str = Field(min_length=1, max_length=1_000)
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=8_000)
    ]


class AgentCitationResponse(BaseModel):
    path: str
    line: int
    end_line: int
    content_hash: str


class AgentEvidenceResponse(BaseModel):
    path: str
    line: int
    end_line: int


class AgentClaimResponse(BaseModel):
    text: str
    citations: tuple[AgentCitationResponse, ...]


class AgentSearchUsageResponse(BaseModel):
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_usd: float | None
    price_table_version: int
    requests: int


class AgentSearchResponse(BaseModel):
    claims: tuple[AgentClaimResponse, ...]
    evidence: tuple[AgentEvidenceResponse, ...]
    complete: bool
    limit_reason: str | None
    message: str
    turns: int
    tool_calls: int
    bytes_read: int
    usage: AgentSearchUsageResponse

    @classmethod
    def from_result(cls, result: SearchAnswer) -> AgentSearchResponse:
        counts = result.usage.counts
        return cls(
            claims=tuple(
                AgentClaimResponse(
                    text=claim.text,
                    citations=tuple(
                        AgentCitationResponse(
                            path=citation.path,
                            line=citation.line,
                            end_line=citation.end_line,
                            content_hash=citation.content_hash,
                        )
                        for citation in claim.citations
                    ),
                )
                for claim in result.claims
            ),
            evidence=tuple(
                AgentEvidenceResponse(
                    path=span.path, line=span.line, end_line=span.end_line
                )
                for span in result.evidence
            ),
            complete=result.complete,
            limit_reason=(
                None if result.limit_reason is None else result.limit_reason.value
            ),
            message=result.message,
            turns=result.turns,
            tool_calls=result.tool_calls,
            bytes_read=result.bytes_read,
            usage=AgentSearchUsageResponse(
                model=result.usage.model,
                input_tokens=counts.input_tokens,
                output_tokens=counts.output_tokens,
                cache_read_tokens=counts.cache_read_tokens,
                cache_write_tokens=counts.cache_write_tokens,
                total_tokens=counts.total,
                cost_usd=result.usage.cost_usd,
                price_table_version=result.usage.price_table_version,
                requests=result.usage.requests,
            ),
        )


class FileLineResponse(BaseModel):
    line: int
    text: str


class FileReadResponse(BaseModel):
    path: str
    lines: tuple[FileLineResponse, ...]
    truncated: bool
    content_hash: str

    @classmethod
    def from_result(cls, result: FileReadResult) -> FileReadResponse:
        return cls(
            path=result.path,
            lines=tuple(
                FileLineResponse(line=line.line, text=line.text)
                for line in result.lines
            ),
            truncated=result.truncated,
            content_hash=result.content_hash,
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


class SearchBranchResponse(BaseModel):
    name: str
    commit: str
    is_head: bool


class SearchProjectResponse(BaseModel):
    id: str
    name: str
    repo_path: str
    branches: tuple[SearchBranchResponse, ...]


class SearchProjectsResponse(BaseModel):
    projects: tuple[SearchProjectResponse, ...]

    @classmethod
    def from_result(cls, result: ProjectCatalogResult) -> SearchProjectsResponse:
        return cls(
            projects=tuple(
                SearchProjectResponse(
                    id=project.id,
                    name=project.name,
                    repo_path=str(project.repo_path),
                    branches=tuple(
                        SearchBranchResponse(
                            name=branch.name,
                            commit=branch.commit,
                            is_head=branch.is_head,
                        )
                        for branch in project.branches
                    ),
                )
                for project in result.projects
            )
        )


def _service(request: Request) -> CodeSearchService:
    return cast(CodeSearchService, request.app.state.search)


def _agent_factory(request: Request) -> Callable[[], Awaitable[SearchAgent]]:
    return cast(
        Callable[[], Awaitable[SearchAgent]],
        request.app.state.search_agent_factory,
    )


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


@router.post("/answer", response_model=AgentSearchResponse)
async def answer_question(
    request: Request, body: AgentSearchRequest
) -> AgentSearchResponse:
    try:
        agent = await _agent_factory(request)()
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    try:
        result = await agent.answer(
            body.project_id,
            body.branch,
            body.question,
        )
    except (
        SearchTargetNotFound,
        SearchPathError,
        SearchOutputTooLarge,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        _raise_http(error)
    finally:
        await agent.close()
    return AgentSearchResponse.from_result(result)


@router.get("/projects", response_model=SearchProjectsResponse)
async def list_projects(request: Request) -> SearchProjectsResponse:
    """Discover known repositories and their local branches."""
    try:
        result = await _service(request).list_projects()
    except (
        SearchTargetNotFound,
        SearchOutputTooLarge,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        _raise_http(error)
    return SearchProjectsResponse.from_result(result)


@router.get("/text", response_model=TextSearchResponse)
async def search_text(
    request: Request,
    project_id: Annotated[str, Query(min_length=1, max_length=128)],
    branch: Annotated[str, Query(min_length=1, max_length=1_000)],
    pattern: Annotated[str, Query(min_length=1, max_length=1_000)],
    glob: Annotated[str | None, Query(max_length=256)] = None,
    case_sensitive: bool = True,
    literal: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> TextSearchResponse:
    try:
        result = await _service(request).search_text(
            project_id,
            branch,
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
    project_id: Annotated[str, Query(min_length=1, max_length=128)],
    branch: Annotated[str, Query(min_length=1, max_length=1_000)],
    pattern: Annotated[str, Query(min_length=1, max_length=1_000)],
    language: Annotated[str, Query(min_length=1, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> TextSearchResponse:
    try:
        result = await _service(request).search_structural(
            project_id,
            branch,
            pattern,
            language=language,
            limit=limit,
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
    project_id: Annotated[str, Query(min_length=1, max_length=128)],
    branch: Annotated[str, Query(min_length=1, max_length=1_000)],
    path: Annotated[str, Query(min_length=1, max_length=1_000)],
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileReadResponse:
    try:
        result = await _service(request).read_file(
            project_id,
            branch,
            path,
            start_line=start_line,
            end_line=end_line,
        )
    except (
        ValueError,
        SearchTargetNotFound,
        SearchPathError,
        SearchOutputTooLarge,
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
    project_id: Annotated[str, Query(min_length=1, max_length=128)],
    branch: Annotated[str, Query(min_length=1, max_length=1_000)],
    path: Annotated[str, Query(min_length=1, max_length=1_000)] = ".",
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> DirectoryListResponse:
    try:
        result = await _service(request).list_directory(
            project_id,
            branch,
            path,
            limit=limit,
        )
    except (
        SearchTargetNotFound,
        SearchPathError,
        SearchOutputTooLarge,
        SearchToolUnavailable,
        SearchTimedOut,
    ) as error:
        _raise_http(error)
    return DirectoryListResponse.from_result(result)


__all__ = ["router"]
