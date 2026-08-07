"""Session REST transport. Validation and delegation only."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request, status

from app.api.schemas import (
    CreatedSessionResponse,
    CreateSessionRequest,
    DiffResponse,
    MergeResponse,
    NodeResponse,
    RunOutcomeResponse,
    RunResponse,
    SessionResponse,
    node_response,
    run_response,
    session_response,
)
from app.orchestrator.service import (
    InvalidTransitionError,
    ResourceNotFoundError,
    SingleRunService,
)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


def _service(request: Request) -> SingleRunService:
    return cast(SingleRunService, request.app.state.orchestrator)


async def _call[T](operation: Awaitable[T]) -> T:
    try:
        return await operation
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "",
    response_model=CreatedSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    body: CreateSessionRequest, request: Request
) -> CreatedSessionResponse:
    result = await _call(
        _service(request).create_session(
            repo_path=body.repo_path,
            prompt=body.prompt,
            harness=body.harness,
            model=body.model,
            title=body.title,
            acceptance_criteria=body.acceptance_criteria,
            auto_merge=body.auto_merge,
            base_ref=body.base_ref,
        )
    )
    return CreatedSessionResponse.from_result(result)


@router.get("", response_model=list[SessionResponse])
async def list_sessions(
    request: Request, limit: int = Query(default=100, ge=1, le=200)
) -> list[SessionResponse]:
    rows = await _call(_service(request).list_sessions(limit=limit))
    return [session_response(row) for row in rows]


@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, request: Request) -> SessionResponse:
    return session_response(await _call(_service(request).get_session(session_id)))


@router.get("/{session_id}/node", response_model=NodeResponse)
async def get_node(session_id: str, request: Request) -> NodeResponse:
    return node_response(await _call(_service(request).get_node(session_id)))


@router.post("/{session_id}/runs", response_model=RunOutcomeResponse)
async def start_run(session_id: str, request: Request) -> RunOutcomeResponse:
    return RunOutcomeResponse.from_result(
        await _call(_service(request).run(session_id))
    )


@router.get("/{session_id}/runs", response_model=list[RunResponse])
async def list_runs(session_id: str, request: Request) -> list[RunResponse]:
    rows = await _call(_service(request).list_runs(session_id))
    return [run_response(row) for row in rows]


@router.post("/{session_id}/approve", response_model=MergeResponse)
async def approve(session_id: str, request: Request) -> MergeResponse:
    return MergeResponse.from_result(await _call(_service(request).approve(session_id)))


@router.get("/{session_id}/diff", response_model=DiffResponse)
async def get_diff(session_id: str, request: Request) -> DiffResponse:
    return DiffResponse(patch=await _call(_service(request).get_diff(session_id)))
