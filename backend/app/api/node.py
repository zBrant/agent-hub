"""Node-addressed REST transport. Validation and delegation only.

**Why this module exists.** C3 re-keyed
:class:`~app.orchestrator.service.NodeRunService` from session to node, because
a node is what genuinely admits one run at a time — one worktree, one live
process — while a session is explicitly meant to have several. Phase 1's routes
stayed session-addressed and, on a graph, resolved through
``_session_and_node``, which refuses with **409** rather than guessing which of
four nodes ``/diff`` meant. Correct, and it left graph sessions with no HTTP
surface. These routes are that surface: the URL now carries the same key the
service does.

**The session-addressed routes in ``session.py`` stay.** They are not
duplicates to be tidied away later; they are the Phase 1 contract that
``docs/acceptance-phase-1.md`` records a real accepted run against, and
``frontend/src/api/client.ts`` calls them today. On a one-node session they
resolve to exactly these operations. On a multi-node session they still return
409 — which is now a signpost rather than a dead end, because the node routes
exist to point at.

Nothing here inspects a node's status. Every refusal below is an
``InvalidTransitionError`` raised by the orchestrator and translated by
:func:`app.api.deps.call` (`docs/architecture.md` §1 rule 3).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import JSONResponse

from app.api.deps import call, resolve_node, scheduler, service
from app.api.schemas import (
    AcceptanceResultResponse,
    DiffResponse,
    GraphResponse,
    MergeResponse,
    NodeResponse,
    NodeReviewResponse,
    RejectRequest,
    RetryRequest,
    ReviewOutcomesRequest,
    RunOutcomeResponse,
    RunResponse,
    RunSummaryResponse,
    UpdateNodeRequest,
    acceptance_response,
    node_response,
    review_response,
    run_response,
)
from app.harnesses.events import agent_event_adapter

router = APIRouter(prefix="/api/sessions/{session_id}/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeResponse])
async def list_nodes(session_id: str, request: Request) -> list[NodeResponse]:
    rows = await call(service(request).list_nodes(session_id))
    return [node_response(row) for row in rows]


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(session_id: str, node_id: str, request: Request) -> NodeResponse:
    return node_response(await resolve_node(request, session_id, node_id))


@router.put("/{node_id}", response_model=NodeResponse)
async def update_node(
    session_id: str, node_id: str, request: Request, body: UpdateNodeRequest
) -> NodeResponse:
    node = await resolve_node(request, session_id, node_id)
    updated = await call(
        service(request).update_node(
            node.id,
            name=body.name,
            prompt=body.prompt,
            harness=body.harness,
            model=body.model,
            acceptance_criteria=body.acceptance_criteria,
            touches=body.touches,
            estimated_effort=body.estimated_effort,
            requires_review=body.requires_review,
        )
    )
    return node_response(updated)


@router.delete("/{node_id}", response_model=GraphResponse)
async def delete_node(session_id: str, node_id: str, request: Request) -> GraphResponse:
    node = await resolve_node(request, session_id, node_id)
    return GraphResponse.from_result(await call(service(request).delete_node(node.id)))


@router.put("/{node_id}/dependencies/{depends_on_id}", response_model=GraphResponse)
async def add_dependency(
    session_id: str,
    node_id: str,
    depends_on_id: str,
    request: Request,
) -> GraphResponse:
    node = await resolve_node(request, session_id, node_id)
    await resolve_node(request, session_id, depends_on_id)
    return GraphResponse.from_result(
        await call(service(request).add_dependency(node.id, depends_on_id))
    )


@router.delete("/{node_id}/dependencies/{depends_on_id}", response_model=GraphResponse)
async def remove_dependency(
    session_id: str,
    node_id: str,
    depends_on_id: str,
    request: Request,
) -> GraphResponse:
    node = await resolve_node(request, session_id, node_id)
    return GraphResponse.from_result(
        await call(service(request).remove_dependency(node.id, depends_on_id))
    )


@router.post("/{node_id}/runs", response_model=RunOutcomeResponse)
async def run_node(
    session_id: str, node_id: str, request: Request
) -> RunOutcomeResponse:
    """Run one already-materialized node to its terminal or gated state.

    Synchronous, exactly as Phase 1's ``POST /runs`` is: the response *is* the
    outcome. See the module note on :func:`reject_node` for why that is a
    deliberate cost here and a reported problem there.
    """
    node = await resolve_node(request, session_id, node_id)
    outcome = await call(service(request).run_node(node.id))
    return RunOutcomeResponse.from_result(outcome)


@router.get("/{node_id}/runs", response_model=list[RunResponse])
async def list_runs(
    session_id: str, node_id: str, request: Request
) -> list[RunResponse]:
    node = await resolve_node(request, session_id, node_id)
    rows = await call(service(request).list_node_runs(node.id))
    return [run_response(row) for row in rows]


@router.get("/{node_id}/runs/{run_id}/summary", response_model=RunSummaryResponse)
async def get_run_summary(
    session_id: str, node_id: str, run_id: str, request: Request
) -> RunSummaryResponse:
    node = await resolve_node(request, session_id, node_id)
    return RunSummaryResponse.from_result(
        await call(service(request).get_node_run_summary(node.id, run_id))
    )


@router.get(
    "/{node_id}/runs/{run_id}/events",
    response_class=JSONResponse,
    responses={200: {"description": "Canonical persisted AgentEvent array"}},
)
async def list_run_events(
    session_id: str, node_id: str, run_id: str, request: Request
) -> Response:
    node = await resolve_node(request, session_id, node_id)
    events = await call(service(request).list_node_run_events(node.id, run_id))
    return JSONResponse(
        [agent_event_adapter.dump_python(event, mode="json") for event in events]
    )


@router.get("/{node_id}/diff", response_model=DiffResponse)
async def get_diff(session_id: str, node_id: str, request: Request) -> DiffResponse:
    node = await resolve_node(request, session_id, node_id)
    return DiffResponse(patch=await call(service(request).get_node_diff(node.id)))


@router.post("/{node_id}/kill", response_model=RunResponse)
async def kill_node(session_id: str, node_id: str, request: Request) -> RunResponse:
    node = await resolve_node(request, session_id, node_id)
    run = await call(service(request).kill_node(node.id))
    return run_response(run)


@router.post("/{node_id}/retry", response_model=RunOutcomeResponse)
async def retry_node(
    session_id: str,
    node_id: str,
    request: Request,
    body: RetryRequest | None = None,
) -> RunOutcomeResponse:
    node = await resolve_node(request, session_id, node_id)
    feedback = None if body is None else body.feedback
    outcome = await call(service(request).retry_node(node.id, feedback=feedback))
    return RunOutcomeResponse.from_result(outcome)


@router.post("/{node_id}/approve", response_model=MergeResponse)
async def approve_node(
    session_id: str,
    node_id: str,
    request: Request,
    body: ReviewOutcomesRequest | None = None,
) -> MergeResponse:
    """The human gate's yes: record the verdict, then merge (invariant 6)."""
    node = await resolve_node(request, session_id, node_id)
    outcomes = None if body is None else body.outcomes
    merge = await call(service(request).approve_node(node.id, outcomes=outcomes))
    scheduler(request).schedule_graph(session_id)
    return MergeResponse.from_result(merge)


@router.post(
    "/{node_id}/reject",
    response_model=NodeReviewResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reject_node(
    session_id: str, node_id: str, request: Request, body: RejectRequest
) -> NodeReviewResponse:
    """Persist the rejection, then let the graph scheduler own the retry."""
    node = await resolve_node(request, session_id, node_id)
    review = await call(
        service(request).reject_node(
            node.id, feedback=body.feedback, outcomes=body.outcomes
        )
    )
    scheduler(request).schedule_graph(session_id)
    return review_response(review)


@router.get("/{node_id}/acceptance", response_model=list[AcceptanceResultResponse])
async def list_acceptance_results(
    session_id: str,
    node_id: str,
    request: Request,
    attempt: int | None = Query(default=None, ge=1),
) -> list[AcceptanceResultResponse]:
    """The per-criterion checklist, oldest attempt first.

    Without ``attempt`` this is the whole history, which is what a drawer
    showing "attempt 2 fixed the criterion attempt 1 failed" needs. With it,
    one attempt — the panel `design.md` §8 specifies for ``awaiting_review``.
    """
    node = await resolve_node(request, session_id, node_id)
    rows = await call(service(request).acceptance_results(node.id, attempt=attempt))
    return [acceptance_response(row) for row in rows]


@router.patch(
    "/{node_id}/acceptance/{attempt}",
    response_model=list[AcceptanceResultResponse],
)
async def resolve_acceptance_results(
    session_id: str,
    node_id: str,
    attempt: int,
    request: Request,
    body: ReviewOutcomesRequest,
) -> list[AcceptanceResultResponse]:
    node = await resolve_node(request, session_id, node_id)
    rows = await call(
        service(request).resolve_acceptance_results(
            node.id, attempt=attempt, outcomes=body.outcomes
        )
    )
    return [acceptance_response(row) for row in rows]


@router.get("/{node_id}/reviews", response_model=list[NodeReviewResponse])
async def list_reviews(
    session_id: str, node_id: str, request: Request
) -> list[NodeReviewResponse]:
    node = await resolve_node(request, session_id, node_id)
    rows = await call(service(request).reviews(node.id))
    return [review_response(row) for row in rows]


__all__ = ["router"]
