"""Graph-resource REST transport. Validation and delegation only.

A "graph" is a session whose nodes are many and whose edges are authored — the
same :class:`~app.models.tables.Session` Phase 1 creates, so it is read through
``/api/sessions`` and its nodes through ``/api/sessions/{id}/nodes``. Only
*creation* needs a resource of its own, because creating a whole DAG in one
transaction is not the same operation as creating a one-node session.

**Shaped for a planner proposal, deliberately.** C8 is writing
``orchestrator/planner.py`` in parallel and this activity does not wire HTTP to
it. What it does is make sure that wiring is a route and not a redesign:
:class:`~app.api.schemas.PlannedNodeRequest` is `design.md` §8's node schema
with the two ``suggested_*`` fields already collapsed onto ``harness``/``model``
(§8 says the suggestion is not retained once answered), ``depends_on`` is by
planner slug, and the body carries ``auto_merge`` so a proposal is persisted
gated. When the planner lands, ``POST /api/graphs/plan`` builds the same
``PlannedNode`` sequence and calls the same
:meth:`~app.orchestrator.service.NodeRunService.create_graph`; the correction
loop stays inside the planner, and a graph that is still invalid after it fails
with the same typed defects this route already returns as 422.

**Invariant 6 holds by construction here.** ``create_graph`` writes every node
``pending`` and materializes nothing: persisting a proposal starts no worktree
and no agent. What is *missing* — editing the proposal, approving it, and
running it — is missing because ``orchestrator/`` exposes no use case for any
of the three; see C9's report. There is no version of those routes that this
module could hold without becoming the state machine itself.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import call, scheduler, service
from app.api.schemas import (
    CreatedGraphResponse,
    CreateGraphRequest,
    GraphResponse,
    GraphRunResponse,
)

router = APIRouter(prefix="/api/graphs", tags=["graphs"])


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
