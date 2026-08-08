"""Graph-resource REST transport. Validation and delegation only.

A "graph" is a session whose nodes are many and whose edges are authored — the
same :class:`~app.models.tables.Session` Phase 1 creates, so it is read through
``/api/sessions`` and its nodes through ``/api/sessions/{id}/nodes``. Only
*creation* needs a resource of its own, because creating a whole DAG in one
transaction is not the same operation as creating a one-node session.

**The planner and hand-authored paths converge here.**
``POST /api/graphs/plan`` asks the planner for a validated proposal, while
``POST /api/graphs`` accepts an already-authored one. Both use
:class:`~app.api.schemas.PlannedNodeRequest`, `design.md` §8's node schema,
with the two ``suggested_*`` fields already collapsed onto ``harness``/``model``
(§8 says the suggestion is not retained once answered), ``depends_on`` is by
planner slug, and the body carries ``auto_merge`` so a proposal is persisted
gated. The planner route builds the same
``PlannedNode`` sequence and calls the same
:meth:`~app.orchestrator.service.NodeRunService.create_graph`; the correction
loop stays inside the planner, and a graph that is still invalid fails with
typed defects and no partial graph.

**Invariant 6 holds by construction here.** ``create_graph`` writes every node
``pending`` and materializes nothing: persisting a proposal starts no worktree
and no agent. Editing remains node-addressed, and approval plus scheduling call
orchestrator use cases below; the transport never owns the state machine.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import call, planner, scheduler, service
from app.api.schemas import (
    CreatedGraphResponse,
    CreateGraphRequest,
    GraphResponse,
    GraphRunResponse,
    PlanGraphRequest,
    PlannedGraphResponse,
    PlannerUsageResponse,
)
from app.orchestrator.planner import PlanFailure

router = APIRouter(prefix="/api/graphs", tags=["graphs"])

# Everything not listed is 422: the model answered, and what came back is
# unprocessable rather than a transport or configuration problem.
_PLAN_FAILURE_STATUS = {"api_error": 502, "not_configured": 503}


@router.post(
    "/plan",
    response_model=PlannedGraphResponse,
    status_code=status.HTTP_201_CREATED,
)
async def plan_graph(body: PlanGraphRequest, request: Request) -> PlannedGraphResponse:
    """Plan and persist an objective as a proposal; never approve or run it."""
    result = await planner(request).plan_graph(
        body.objective,
        repo_path=body.repo_path,
        creator=service(request),
        context=body.context,
        auto_merge=body.auto_merge,
        base_ref=body.base_ref,
    )
    if isinstance(result, PlanFailure):
        detail = {
            "kind": result.kind.value,
            "message": result.message,
            "attempts": result.attempts,
            "planner_usage": PlannerUsageResponse.from_result(
                result.usage
            ).model_dump(),
            "errors": [
                {
                    "kind": error.kind.value,
                    "nodes": list(error.nodes),
                    "message": error.message,
                }
                for error in result.errors
            ],
        }
        # 503 and not 502: with no credential nothing upstream was reached, and
        # the fix is on this machine — an operator reading "bad gateway" would
        # go looking for an Anthropic outage.
        code = _PLAN_FAILURE_STATUS.get(result.kind.value, 422)
        raise HTTPException(status_code=code, detail=detail)
    return PlannedGraphResponse.from_proposal(result)


@router.post(
    "",
    response_model=CreatedGraphResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_graph(
    body: CreateGraphRequest, request: Request
) -> CreatedGraphResponse:
    """Persist a proposed DAG: one session, its nodes, and its edges.

    422 with the typed defects when the proposal is not a DAG — a cycle, a
    ``depends_on`` naming a slug that is not in the body, a duplicate name. The
    validation happens before the first row, so a rejected proposal leaves
    nothing behind (the alternative, half a graph on disk, is worse than none).
    """
    result = await call(
        service(request).create_graph(
            repo_path=body.repo_path,
            nodes=[node.to_planned() for node in body.nodes],
            title=body.title,
            auto_merge=body.auto_merge,
            base_ref=body.base_ref,
        )
    )
    return CreatedGraphResponse.from_result(result)


@router.get("/{session_id}", response_model=GraphResponse)
async def get_graph(session_id: str, request: Request) -> GraphResponse:
    return GraphResponse.from_result(await call(service(request).get_graph(session_id)))


@router.post("/{session_id}/approve", response_model=GraphResponse)
async def approve_graph(session_id: str, request: Request) -> GraphResponse:
    return GraphResponse.from_result(
        await call(service(request).approve_graph(session_id))
    )


@router.post(
    "/{session_id}/runs",
    response_model=GraphRunResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_graph(session_id: str, request: Request) -> GraphRunResponse:
    await call(service(request).require_graph_approved(session_id))
    return GraphRunResponse(
        session_id=session_id,
        scheduled=scheduler(request).schedule_graph(session_id),
    )


__all__ = ["router"]
