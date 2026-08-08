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

from fastapi import APIRouter, Query, Request

from app.api.deps import announce, call, resolve_node, service
from app.api.schemas import (
    AcceptanceResultResponse,
    MergeResponse,
    NodeResponse,
    NodeReviewResponse,
    RejectRequest,
    RetryRequest,
    ReviewOutcomesRequest,
    RunOutcomeResponse,
    RunResponse,
    acceptance_response,
    node_response,
    review_response,
    run_response,
)

router = APIRouter(prefix="/api/sessions/{session_id}/nodes", tags=["nodes"])


@router.get("", response_model=list[NodeResponse])
async def list_nodes(session_id: str, request: Request) -> list[NodeResponse]:
    rows = await call(service(request).list_nodes(session_id))
    return [node_response(row) for row in rows]


@router.get("/{node_id}", response_model=NodeResponse)
async def get_node(session_id: str, node_id: str, request: Request) -> NodeResponse:
    return node_response(await resolve_node(request, session_id, node_id))


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
    await announce(request, await resolve_node(request, session_id, node_id))
    return RunOutcomeResponse.from_result(outcome)


@router.post("/{node_id}/kill", response_model=RunResponse)
async def kill_node(session_id: str, node_id: str, request: Request) -> RunResponse:
    node = await resolve_node(request, session_id, node_id)
    run = await call(service(request).kill_node(node.id))
    await announce(request, await resolve_node(request, session_id, node_id))
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
    await announce(request, await resolve_node(request, session_id, node_id))
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
    await announce(request, await resolve_node(request, session_id, node_id))
    return MergeResponse.from_result(merge)


@router.post("/{node_id}/reject", response_model=RunOutcomeResponse)
async def reject_node(
    session_id: str, node_id: str, request: Request, body: RejectRequest
) -> RunOutcomeResponse:
    """The human gate's no: record the verdict, then retry with feedback.

    **This request is held open for the whole retry, and that is a reported
    defect rather than a design.** C7 flagged it for C9 to decide, and the
    decision is that the retry should be *scheduled* — the verdict is durable
    before the transition to ``ready``, so a process that dies between them
    restarts into a node the scheduler picks up carrying the feedback anyway.

    It is not implemented here because it cannot be implemented here honestly.
    ``reject_node`` is one call that performs the state check, writes the
    verdict, transitions the node and then runs an agent; the transport has no
    point between the fourth and the fifth at which it could answer 202. The
    two ways to fake it are both worse than the wait: returning 202 before the
    check means an invalid transition arrives as an unobserved task exception
    instead of a 409, and pre-checking the status in this function puts the
    state machine in a route (`docs/architecture.md` §1 rule 3). The fix is a
    service method that returns after the transition; see C9's report.
    """
    node = await resolve_node(request, session_id, node_id)
    outcome = await call(
        service(request).reject_node(
            node.id, feedback=body.feedback, outcomes=body.outcomes
        )
    )
    await announce(request, await resolve_node(request, session_id, node_id))
    return RunOutcomeResponse.from_result(outcome)


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


@router.get("/{node_id}/reviews", response_model=list[NodeReviewResponse])
async def list_reviews(
    session_id: str, node_id: str, request: Request
) -> list[NodeReviewResponse]:
    node = await resolve_node(request, session_id, node_id)
    rows = await call(service(request).reviews(node.id))
    return [review_response(row) for row in rows]


__all__ = ["router"]
