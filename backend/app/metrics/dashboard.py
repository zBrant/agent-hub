"""Read-only dashboard projection over durable orchestration data.

This vertical owns presentation-oriented aggregation, not orchestration state.
All token and cost arithmetic stays in SQLite, and cost is only summed from the
ingest-time values already stored on ``usage_event`` (invariants 3 and 7).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from sqlalchemy import func
from sqlmodel import col, select

from app.models.clock import now_ms
from app.models.pricing import TokenCounts
from app.models.status import NodeStatus, SessionStatus
from app.models.tables import Node, NodeTransition, Session, UsageEvent
from app.storage.db import Database

DAY_MS = 86_400_000


class DashboardPeriod(StrEnum):
    TODAY = "today"
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"

    def since_ms(self, end_ms: int) -> int:
        if self is DashboardPeriod.TODAY:
            # UTC throughout the backend (`docs/conventions.md` §2). The
            # frontend formats the boundary in the operator's timezone.
            return end_ms - (end_ms % DAY_MS)
        days = 7 if self is DashboardPeriod.SEVEN_DAYS else 30
        return end_ms - days * DAY_MS


@dataclass(frozen=True, slots=True)
class MetricUsage:
    key: str
    counts: TokenCounts
    cost_usd: float | None
    events: int
    unpriced_events: int

    @property
    def cost_complete(self) -> bool:
        return self.unpriced_events == 0


@dataclass(frozen=True, slots=True)
class ActiveSessionMetric:
    id: str
    title: str
    status: SessionStatus
    created_ms: int
    elapsed_ms: int
    total_nodes: int
    completed_nodes: int
    blocked_nodes: int
    harnesses: tuple[str, ...]
    usage: MetricUsage


@dataclass(frozen=True, slots=True)
class DashboardTransition:
    id: int
    session_id: str
    session_title: str
    node_id: str
    node_name: str
    status: NodeStatus
    ts: int


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    period: DashboardPeriod
    since_ms: int
    generated_ms: int
    usage: MetricUsage
    by_harness: tuple[MetricUsage, ...]
    by_model: tuple[MetricUsage, ...]
    active_sessions: tuple[ActiveSessionMetric, ...]
    active_session_count: int
    running_node_count: int
    blocked_node_count: int
    node_completion_rate: float | None
    event_feed: tuple[DashboardTransition, ...]


_ACTIVE_SESSION_STATUSES = (
    SessionStatus.PLANNING,
    SessionStatus.RUNNING,
    SessionStatus.PAUSED,
)
_COMPLETED_NODE_STATUSES = (NodeStatus.DONE, NodeStatus.SKIPPED)
_DECIDED_NODE_STATUSES = (*_COMPLETED_NODE_STATUSES, NodeStatus.FAILED)
_MEANINGFUL_NODE_STATUSES = (
    NodeStatus.AWAITING_REVIEW,
    NodeStatus.BLOCKED,
    NodeStatus.DONE,
    NodeStatus.FAILED,
    NodeStatus.SKIPPED,
)
EVENT_FEED_LIMIT = 20


def _usage_columns() -> tuple[Any, ...]:
    return (
        func.coalesce(func.sum(col(UsageEvent.input_tokens)), 0),
        func.coalesce(func.sum(col(UsageEvent.output_tokens)), 0),
        func.coalesce(func.sum(col(UsageEvent.cache_read_tokens)), 0),
        func.coalesce(func.sum(col(UsageEvent.cache_write_tokens)), 0),
        func.coalesce(func.sum(col(UsageEvent.cache_write_5m_tokens)), 0),
        func.coalesce(func.sum(col(UsageEvent.cache_write_1h_tokens)), 0),
        func.sum(col(UsageEvent.cost_usd)),
        func.count(),
        func.count(col(UsageEvent.cost_usd)),
    )


def _metric_usage(key: str, row: Any) -> MetricUsage:
    events = int(row[7])
    return MetricUsage(
        key=key,
        counts=TokenCounts(
            input_tokens=int(row[0]),
            output_tokens=int(row[1]),
            cache_read_tokens=int(row[2]),
            cache_write_tokens=int(row[3]),
            cache_write_5m_tokens=int(row[4]),
            cache_write_1h_tokens=int(row[5]),
        ),
        cost_usd=None if row[6] is None else float(row[6]),
        events=events,
        unpriced_events=events - int(row[8]),
    )


class DashboardService:
    """Build one consistent dashboard snapshot from the derived index."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def snapshot(
        self, period: DashboardPeriod = DashboardPeriod.TODAY
    ) -> DashboardSnapshot:
        generated = now_ms()
        since = period.since_ms(generated)
        async with self._database.session() as db_session:
            total_row = (
                await db_session.exec(
                    select(*_usage_columns()).where(col(UsageEvent.ts) >= since)
                )
            ).one()
            usage = _metric_usage("total", total_row)
            by_harness = await self._breakdown(
                db_session, col(UsageEvent.harness), since
            )
            by_model = await self._breakdown(db_session, col(UsageEvent.model), since)

            sessions = tuple(
                (
                    await db_session.exec(
                        select(Session)
                        .where(col(Session.status).in_(_ACTIVE_SESSION_STATUSES))
                        .order_by(col(Session.updated_ms).desc())
                    )
                ).all()
            )
            nodes = await self._active_nodes(db_session, sessions)
            session_usage = await self._session_usage(db_session, sessions)
            active = self._active_sessions(sessions, nodes, session_usage, generated)

            running = await self._node_count(db_session, NodeStatus.RUNNING)
            blocked = await self._node_count(db_session, NodeStatus.BLOCKED)
            completion = await self._completion_rate(db_session, since)
            event_feed = await self._event_feed(db_session)

        return DashboardSnapshot(
            period=period,
            since_ms=since,
            generated_ms=generated,
            usage=usage,
            by_harness=by_harness,
            by_model=by_model,
            active_sessions=active,
            active_session_count=len(active),
            running_node_count=running,
            blocked_node_count=blocked,
            node_completion_rate=completion,
            event_feed=event_feed,
        )

    @staticmethod
    async def _breakdown(
        db_session: Any, group: Any, since: int
    ) -> tuple[MetricUsage, ...]:
        rows = (
            await db_session.exec(
                select(group, *_usage_columns())
                .where(col(UsageEvent.ts) >= since)
                .group_by(group)
                .order_by(group)
            )
        ).all()
        return tuple(_metric_usage(str(row[0]), row[1:]) for row in rows)

    @staticmethod
    async def _active_nodes(
        db_session: Any, sessions: tuple[Session, ...]
    ) -> tuple[Node, ...]:
        if not sessions:
            return ()
        ids = [session.id for session in sessions]
        return tuple(
            (
                await db_session.exec(
                    select(Node)
                    .where(col(Node.session_id).in_(ids))
                    .order_by(col(Node.id))
                )
            ).all()
        )

    @staticmethod
    async def _session_usage(
        db_session: Any, sessions: tuple[Session, ...]
    ) -> dict[str, MetricUsage]:
        if not sessions:
            return {}
        ids = [session.id for session in sessions]
        group = col(UsageEvent.session_id)
        rows = (
            await db_session.exec(
                select(group, *_usage_columns()).where(group.in_(ids)).group_by(group)
            )
        ).all()
        return {str(row[0]): _metric_usage(str(row[0]), row[1:]) for row in rows}

    @staticmethod
    def _active_sessions(
        sessions: tuple[Session, ...],
        nodes: tuple[Node, ...],
        usage: dict[str, MetricUsage],
        generated: int,
    ) -> tuple[ActiveSessionMetric, ...]:
        by_session: dict[str, list[Node]] = {session.id: [] for session in sessions}
        for node in nodes:
            by_session[node.session_id].append(node)
        return tuple(
            ActiveSessionMetric(
                id=session.id,
                title=session.title,
                status=session.status,
                created_ms=session.created_ms,
                elapsed_ms=max(0, generated - session.created_ms),
                total_nodes=len(by_session[session.id]),
                completed_nodes=sum(
                    node.status in _COMPLETED_NODE_STATUSES
                    for node in by_session[session.id]
                ),
                blocked_nodes=sum(
                    node.status is NodeStatus.BLOCKED for node in by_session[session.id]
                ),
                harnesses=tuple(
                    sorted({node.harness for node in by_session[session.id]})
                ),
                usage=usage.get(session.id)
                or MetricUsage(
                    key=session.id,
                    counts=TokenCounts(),
                    cost_usd=None,
                    events=0,
                    unpriced_events=0,
                ),
            )
            for session in sessions
        )

    @staticmethod
    async def _node_count(db_session: Any, status: NodeStatus) -> int:
        statement = select(func.count()).select_from(Node).where(Node.status == status)
        return int((await db_session.exec(statement)).one())

    @staticmethod
    async def _completion_rate(db_session: Any, since: int) -> float | None:
        rows = (
            await db_session.exec(
                select(col(Node.status), func.count())
                .where(col(Node.updated_ms) >= since)
                .where(col(Node.status).in_(_DECIDED_NODE_STATUSES))
                .group_by(col(Node.status))
            )
        ).all()
        counts = {NodeStatus(row[0]): int(row[1]) for row in rows}
        decided = sum(counts.values())
        if not decided:
            return None
        completed = sum(counts.get(status, 0) for status in _COMPLETED_NODE_STATUSES)
        return completed / decided

    @staticmethod
    async def _event_feed(db_session: Any) -> tuple[DashboardTransition, ...]:
        rows = (
            await db_session.exec(
                select(NodeTransition, col(Session.title), col(Node.name))
                .join(Session, col(Session.id) == col(NodeTransition.session_id))
                .join(Node, col(Node.id) == col(NodeTransition.node_id))
                .where(col(NodeTransition.status).in_(_MEANINGFUL_NODE_STATUSES))
                .order_by(col(NodeTransition.ts).desc(), col(NodeTransition.id).desc())
                .limit(EVENT_FEED_LIMIT)
            )
        ).all()
        return tuple(
            DashboardTransition(
                id=int(transition.id),
                session_id=transition.session_id,
                session_title=str(session_title),
                node_id=transition.node_id,
                node_name=str(node_name),
                status=transition.status,
                ts=transition.ts,
            )
            for transition, session_title, node_name in rows
            if transition.id is not None
        )


__all__ = [
    "ActiveSessionMetric",
    "DashboardPeriod",
    "DashboardService",
    "DashboardSnapshot",
    "DashboardTransition",
    "MetricUsage",
]
