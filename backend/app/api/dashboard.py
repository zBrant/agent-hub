"""Dashboard REST projection. Read-only and presentation-shaped."""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.api.schemas import TokenCountsResponse
from app.metrics.dashboard import (
    ActiveSessionMetric,
    DashboardPeriod,
    DashboardService,
    DashboardSnapshot,
    MetricUsage,
)
from app.models.status import SessionStatus

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class MetricUsageResponse(BaseModel):
    key: str
    tokens: TokenCountsResponse
    estimated_equivalent_cost_usd: float | None
    cost_complete: bool

    @classmethod
    def from_result(cls, result: MetricUsage) -> MetricUsageResponse:
        counts = result.counts
        return cls(
            key=result.key,
            tokens=TokenCountsResponse(
                input_tokens=counts.input_tokens,
                output_tokens=counts.output_tokens,
                cache_read_tokens=counts.cache_read_tokens,
                cache_write_tokens=counts.cache_write_tokens,
                total_tokens=counts.total,
            ),
            estimated_equivalent_cost_usd=result.cost_usd,
            cost_complete=result.cost_complete,
        )


class ActiveSessionMetricResponse(BaseModel):
    id: str
    title: str
    status: SessionStatus
    created_ms: int
    elapsed_ms: int
    total_nodes: int
    completed_nodes: int
    blocked_nodes: int
    harnesses: tuple[str, ...]
    usage: MetricUsageResponse

    @classmethod
    def from_result(cls, result: ActiveSessionMetric) -> ActiveSessionMetricResponse:
        return cls(
            id=result.id,
            title=result.title,
            status=result.status,
            created_ms=result.created_ms,
            elapsed_ms=result.elapsed_ms,
            total_nodes=result.total_nodes,
            completed_nodes=result.completed_nodes,
            blocked_nodes=result.blocked_nodes,
            harnesses=result.harnesses,
            usage=MetricUsageResponse.from_result(result.usage),
        )


class DashboardResponse(BaseModel):
    period: DashboardPeriod
    since_ms: int
    generated_ms: int
    usage: MetricUsageResponse
    by_harness: tuple[MetricUsageResponse, ...]
    by_model: tuple[MetricUsageResponse, ...]
    active_sessions: tuple[ActiveSessionMetricResponse, ...]
    active_session_count: int
    running_node_count: int
    blocked_node_count: int
    node_completion_rate: float | None

    @classmethod
    def from_result(cls, result: DashboardSnapshot) -> DashboardResponse:
        return cls(
            period=result.period,
            since_ms=result.since_ms,
            generated_ms=result.generated_ms,
            usage=MetricUsageResponse.from_result(result.usage),
            by_harness=tuple(
                MetricUsageResponse.from_result(row) for row in result.by_harness
            ),
            by_model=tuple(
                MetricUsageResponse.from_result(row) for row in result.by_model
            ),
            active_sessions=tuple(
                ActiveSessionMetricResponse.from_result(row)
                for row in result.active_sessions
            ),
            active_session_count=result.active_session_count,
            running_node_count=result.running_node_count,
            blocked_node_count=result.blocked_node_count,
            node_completion_rate=result.node_completion_rate,
        )


@router.get("", response_model=DashboardResponse)
async def get_dashboard(
    request: Request,
    period: DashboardPeriod = DashboardPeriod.TODAY,
) -> DashboardResponse:
    metrics = cast(DashboardService, request.app.state.dashboard)
    return DashboardResponse.from_result(await metrics.snapshot(period))


__all__ = ["router"]
