"""REST wire models generated into the frontend through OpenAPI."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from app.models.status import NodeStatus, RunState, SessionStatus
from app.models.tables import Node, Run, Session
from app.orchestrator.service import CreatedSession, RunOutcome, RunSummary


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
    acceptance_criteria: str | None = None
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
    acceptance_criteria: str | None
    harness: str
    model: str | None
    worktree_path: Path | None
    branch: str | None
    base_ref: str | None
    status: NodeStatus
    created_ms: int
    updated_ms: int


class CreatedSessionResponse(BaseModel):
    session: SessionResponse
    node: NodeResponse

    @classmethod
    def from_result(cls, result: CreatedSession) -> CreatedSessionResponse:
        return cls(
            session=SessionResponse.model_validate(result.session),
            node=NodeResponse.model_validate(result.node),
        )


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
