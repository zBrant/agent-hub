"""REST wire models generated into the frontend through OpenAPI."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models.status import NodeStatus, RunState, SessionStatus
from app.models.tables import (
    AcceptanceResult,
    CriterionOutcome,
    Node,
    NodeReview,
    ReviewDecision,
    Run,
    Session,
)
from app.orchestrator.service import (
    CreatedGraph,
    CreatedSession,
    PlannedNode,
    RunOutcome,
    RunSummary,
)
from app.storage.repository import SessionGraph


class _StatusValue(Protocol):
    @property
    def value(self) -> str: ...


class _MergeResult(Protocol):
    @property
    def status(self) -> _StatusValue: ...

    @property
    def commit(self) -> str | None: ...

    @property
    def conflicts(self) -> Sequence[Path]: ...


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_path: Path
    prompt: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model: str | None = None
    title: str | None = None
    acceptance_criteria: tuple[str, ...] = ()
    auto_merge: bool = False
    base_ref: str = "HEAD"


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    repo_path: Path
    workspace_root: Path
    integration_branch: str
    auto_merge: bool
    status: SessionStatus
    created_ms: int
    updated_ms: int


class NodeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    session_id: str
    name: str
    prompt: str
    acceptance_criteria: tuple[str, ...]
    harness: str
    model: str | None
    # C1's two graph columns. They are authored planner output (`design.md` §8)
    # and the canvas renders both, so a node read that omitted them would send
    # C10 back to the database for a field the row already carries.
    touches: tuple[str, ...]
    estimated_effort: str | None
    worktree_path: Path | None
    branch: str | None
    base_ref: str | None
    status: NodeStatus
    created_ms: int
    updated_ms: int


class UpdateNodeRequest(BaseModel):
    """Complete replacement of a proposal node's authored fields."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    acceptance_criteria: tuple[str, ...] = ()
    harness: str = Field(min_length=1)
    model: str | None = None
    touches: tuple[str, ...] = ()
    estimated_effort: str | None = None


class NodeDependencyResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: str
    depends_on_id: str
    session_id: str
    created_ms: int


class GraphResponse(BaseModel):
    session: SessionResponse
    nodes: tuple[NodeResponse, ...]
    edges: tuple[NodeDependencyResponse, ...]

    @classmethod
    def from_result(cls, result: SessionGraph) -> GraphResponse:
        return cls(
            session=SessionResponse.model_validate(result.session),
            nodes=tuple(NodeResponse.model_validate(node) for node in result.nodes),
            edges=tuple(
                NodeDependencyResponse.model_validate(edge) for edge in result.edges
            ),
        )


class GraphRunResponse(BaseModel):
    session_id: str
    scheduled: bool


class CreatedSessionResponse(BaseModel):
    session: SessionResponse
    node: NodeResponse

    @classmethod
    def from_result(cls, result: CreatedSession) -> CreatedSessionResponse:
        return cls(
            session=SessionResponse.model_validate(result.session),
            node=NodeResponse.model_validate(result.node),
        )


class PlannedNodeRequest(BaseModel):
    """One activity of a proposed graph, keyed by name.

    This is `design.md` §8's planner node schema on the wire, and the field
    names are deliberately the *stored* ones rather than the planner's:
    ``suggested_harness``/``suggested_model`` are a suggestion the operator has
    already answered by the time a graph is created, and §8 says the suggestion
    is not retained. C8's ``orchestrator/planner.py`` therefore maps its
    structured-output model onto this one, and a proposal reaches
    :meth:`~app.orchestrator.service.NodeRunService.create_graph` through the
    same call a hand-authored graph does.

    ``depends_on`` names the *other nodes' ``name`` values*, not ids: the
    planner cannot know the ULIDs the database will allocate, and resolving
    slugs to ids happens in exactly one place (``create_graph``). An
    unresolvable name comes back as a typed ``unknown_dependency`` defect naming
    the slug, not as a 500.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    prompt: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    model: str | None = None
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    touches: tuple[str, ...] = ()
    estimated_effort: str | None = None

    def to_planned(self) -> PlannedNode:
        return PlannedNode(
            name=self.name,
            prompt=self.prompt,
            harness=self.harness,
            model=self.model,
            depends_on=self.depends_on,
            acceptance_criteria=self.acceptance_criteria,
            touches=self.touches,
            estimated_effort=self.estimated_effort,
        )


class CreateGraphRequest(BaseModel):
    """A whole proposed graph, persisted in one call.

    One call and not "create session, then POST each node": a half-written
    graph is a graph, and a scheduler reading one would happily start the
    fragment it can see. ``create_graph`` validates the DAG before the first row
    (invariant 6 — this is a proposal, and persisting it starts nothing).
    """

    model_config = ConfigDict(extra="forbid")

    repo_path: Path
    nodes: tuple[PlannedNodeRequest, ...] = Field(min_length=1)
    title: str | None = None
    auto_merge: bool = False
    base_ref: str = "HEAD"


class CreatedGraphResponse(BaseModel):
    session: SessionResponse
    nodes: tuple[NodeResponse, ...]
    #: planner slug → allocated node id, so a caller that authored ``depends_on``
    #: by name can address the result without re-deriving the mapping.
    ids_by_name: dict[str, str]

    @classmethod
    def from_result(cls, result: CreatedGraph) -> CreatedGraphResponse:
        return cls(
            session=SessionResponse.model_validate(result.session),
            nodes=tuple(NodeResponse.model_validate(node) for node in result.nodes),
            ids_by_name=dict(result.ids_by_name),
        )


class AcceptanceResultResponse(BaseModel):
    """One criterion of `design.md` §8's ``awaiting_review`` checklist."""

    model_config = ConfigDict(from_attributes=True)

    node_id: str
    attempt: int
    position: int
    criterion: str
    outcome: CriterionOutcome
    created_ms: int
    updated_ms: int


class NodeReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    node_id: str
    attempt: int
    decision: ReviewDecision
    feedback: str | None
    reviewed_ms: int


class ReviewOutcomesRequest(BaseModel):
    """The reviewer's answers to the acceptance checklist, by position.

    Partial on purpose: a position left out keeps whatever it had, which is
    ``unevaluated`` until somebody says otherwise. The orchestrator decides
    whether a ``fail`` disqualifies an approval — it does not, deliberately
    (see :meth:`~app.orchestrator.service.NodeRunService.approve_node`) — and
    this transport does not second-guess it.
    """

    model_config = ConfigDict(extra="forbid")

    outcomes: dict[int, CriterionOutcome] = Field(default_factory=dict)


class RejectRequest(ReviewOutcomesRequest):
    #: Required and non-blank; the orchestrator raises on whitespace, which
    #: surfaces as 422. A rejection with no reason produces an attempt that
    #: differs from the last one only by luck.
    feedback: str = Field(min_length=1)


class RetryRequest(BaseModel):
    """A new attempt at a ``failed`` or ``blocked`` node.

    ``feedback`` is optional here and mandatory on reject, and that asymmetry is
    the orchestrator's: retry is "try again", reject is a human overruling a
    finished attempt.
    """

    model_config = ConfigDict(extra="forbid")

    feedback: str | None = None


class RunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    node_id: str
    session_id: str
    attempt: int
    status: RunState
    harness: str
    model: str | None
    pid: int | None
    harness_session_id: str | None
    harness_version: str | None
    started_ms: int | None
    finished_ms: int | None
    exit_code: int | None
    summary: str | None
    event_count: int
    permission_denial_count: int
    created_ms: int


class TokenCountsResponse(BaseModel):
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int


class RunOutcomeResponse(BaseModel):
    session_id: str
    node_id: str
    run_id: str
    run_status: RunState
    node_status: NodeStatus
    trusted: bool
    permission_denials: int
    tokens: TokenCountsResponse
    estimated_equivalent_cost_usd: float | None
    cost_complete: bool
    commit: str | None
    merged: bool
    block_reason: str | None

    @classmethod
    def from_result(cls, result: RunOutcome) -> RunOutcomeResponse:
        counts = result.totals.counts
        return cls(
            session_id=result.session_id,
            node_id=result.node_id,
            run_id=result.run_id,
            run_status=result.run_status,
            node_status=result.node_status,
            trusted=result.trusted,
            permission_denials=result.permission_denials,
            tokens=TokenCountsResponse(
                input_tokens=counts.input_tokens,
                output_tokens=counts.output_tokens,
                cache_read_tokens=counts.cache_read_tokens,
                cache_write_tokens=counts.cache_write_tokens,
                total_tokens=counts.total,
            ),
            estimated_equivalent_cost_usd=result.totals.cost_usd,
            cost_complete=result.totals.complete,
            commit=result.commit.commit,
            merged=result.merge is not None and not result.merge.blocked,
            block_reason=(
                None if result.block_reason is None else result.block_reason.value
            ),
        )


class RunSummaryResponse(BaseModel):
    run_id: str
    trusted: bool
    tokens: TokenCountsResponse
    estimated_equivalent_cost_usd: float | None
    cost_complete: bool

    @classmethod
    def from_result(cls, result: RunSummary) -> RunSummaryResponse:
        counts = result.totals.counts
        return cls(
            run_id=result.run.id,
            trusted=result.trusted,
            tokens=TokenCountsResponse(
                input_tokens=counts.input_tokens,
                output_tokens=counts.output_tokens,
                cache_read_tokens=counts.cache_read_tokens,
                cache_write_tokens=counts.cache_write_tokens,
                total_tokens=counts.total,
            ),
            estimated_equivalent_cost_usd=result.totals.cost_usd,
            cost_complete=result.totals.complete,
        )


class MergeResponse(BaseModel):
    status: str
    commit: str | None
    conflicts: tuple[Path, ...]

    @classmethod
    def from_result(cls, result: _MergeResult) -> MergeResponse:
        return cls(
            status=result.status.value,
            commit=result.commit,
            conflicts=tuple(result.conflicts),
        )


class DiffResponse(BaseModel):
    patch: str


def session_response(row: Session) -> SessionResponse:
    return SessionResponse.model_validate(row)


def node_response(row: Node) -> NodeResponse:
    return NodeResponse.model_validate(row)


def run_response(row: Run) -> RunResponse:
    return RunResponse.model_validate(row)


def acceptance_response(row: AcceptanceResult) -> AcceptanceResultResponse:
    return AcceptanceResultResponse.model_validate(row)


def review_response(row: NodeReview) -> NodeReviewResponse:
    return NodeReviewResponse.model_validate(row)
