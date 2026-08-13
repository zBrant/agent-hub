"""Session REST transport. Validation and delegation only.

**Scope, after C9.** Everything below either addresses the *session* (create,
list, read) or is a Phase 1 convenience that resolves a one-node session to its
only node. The second group — ``/node``, ``/runs``, ``/kill``, ``/retry``,
``/approve``, ``/diff`` — is kept rather than replaced by
``/api/sessions/{id}/nodes/{node_id}/...``, for three reasons:

1. ``docs/acceptance-phase-1.md`` records a real accepted end-to-end run
   against these exact URLs. Deleting them invalidates an acceptance record
   that a later phase is entitled to re-run.
2. ``frontend/src/api/client.ts`` calls them, and C9 may not edit ``frontend/``.
   Removing them here would ship a backend the committed frontend cannot talk
   to, between two activities.
3. Their 409 on a multi-node session was never the problem C3 reported. The
   problem was that there was nothing else to call; ``api/node.py`` is that
   something. Refusing to guess which of four nodes ``/diff`` meant remains the
   right answer, and it is now a redirect instead of a dead end.

Two of them — ``/runs/{run_id}/summary`` and ``/runs/{run_id}/events`` — have no
node-addressed counterpart at all, because a run id already identifies its node
and the service resolves it through the session. They are not conveniences and
are not going anywhere.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import call as _call
from app.api.deps import service as _service
from app.api.schemas import (
    CreatedSessionResponse,
    CreateSessionRequest,
    DiffResponse,
    MergeResponse,
    NodeResponse,
    RunOutcomeResponse,
    RunResponse,
    RunSummaryResponse,
    SessionResponse,
    node_response,
    run_response,
    session_response,
)
from app.harnesses.events import agent_event_adapter

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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
            requires_review=body.requires_review,
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


@router.get("/{session_id}/runs/{run_id}/summary", response_model=RunSummaryResponse)
async def get_run_summary(
    session_id: str, run_id: str, request: Request
) -> RunSummaryResponse:
    return RunSummaryResponse.from_result(
        await _call(_service(request).get_run_summary(session_id, run_id))
    )


@router.get(
    "/{session_id}/runs/{run_id}/events",
    response_class=JSONResponse,
    # AgentEvent remains generated from its canonical JSON Schema, never from
    # a second OpenAPI mirror (architecture §7).
    responses={200: {"description": "Canonical persisted AgentEvent array"}},
)
async def list_run_events(session_id: str, run_id: str, request: Request) -> Response:
    events = await _call(_service(request).list_run_events(session_id, run_id))
    return JSONResponse(
        [agent_event_adapter.dump_python(event, mode="json") for event in events]
    )


@router.post("/{session_id}/kill", response_model=RunResponse)
async def kill_run(session_id: str, request: Request) -> RunResponse:
    return run_response(await _call(_service(request).kill(session_id)))


@router.post("/{session_id}/retry", response_model=RunOutcomeResponse)
async def retry_run(session_id: str, request: Request) -> RunOutcomeResponse:
    return RunOutcomeResponse.from_result(
        await _call(_service(request).retry(session_id))
    )


@router.post("/{session_id}/approve", response_model=MergeResponse)
async def approve(session_id: str, request: Request) -> MergeResponse:
    return MergeResponse.from_result(await _call(_service(request).approve(session_id)))


@router.get("/{session_id}/diff", response_model=DiffResponse)
async def get_diff(session_id: str, request: Request) -> DiffResponse:
    return DiffResponse(patch=await _call(_service(request).get_diff(session_id)))
