"""Pure Phase 1 lifecycle decisions.

Phase 1 has one fixed node rather than a DAG, but it already needs one answer to
"what state follows this run?". Keeping that answer here prevents the service,
the future REST route, and replay from each inventing a slightly different
transition (`docs/architecture.md` §3). Phase 2 will extend this module with DAG
readiness and validation without moving these safety rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.models.status import NodeStatus, RunState, SessionStatus


class RunBlockReason(StrEnum):
    PARSER_UNTRUSTED = "parser_untrusted"
    PERMISSION_DENIED = "permission_denied"
    NO_CHANGES = "no_changes"


@dataclass(frozen=True, slots=True)
class RunDisposition:
    node_status: NodeStatus
    mergeable: bool
    reason: RunBlockReason | None = None


def evaluate_run(
    status: RunState,
    *,
    trusted: bool,
    permission_denials: int,
    changed: bool,
) -> RunDisposition:
    """Turn one terminal run and its checkpoint into the next node state."""
    if status is not RunState.SUCCESS:
        return RunDisposition(NodeStatus.FAILED, mergeable=False)
    if not trusted:
        return RunDisposition(
            NodeStatus.BLOCKED,
            mergeable=False,
            reason=RunBlockReason.PARSER_UNTRUSTED,
        )
    if permission_denials:
        return RunDisposition(
            NodeStatus.BLOCKED,
            mergeable=False,
            reason=RunBlockReason.PERMISSION_DENIED,
        )
    if not changed:
        return RunDisposition(
            NodeStatus.BLOCKED,
            mergeable=False,
            reason=RunBlockReason.NO_CHANGES,
        )
    return RunDisposition(NodeStatus.AWAITING_REVIEW, mergeable=True)


def session_status_for_node(status: NodeStatus) -> SessionStatus:
    """The Phase 1 session projection for its only node."""
    if status in (NodeStatus.PENDING, NodeStatus.READY):
        return SessionStatus.PLANNING
    if status is NodeStatus.RUNNING:
        return SessionStatus.RUNNING
    if status in (NodeStatus.AWAITING_REVIEW, NodeStatus.BLOCKED):
        return SessionStatus.PAUSED
    if status in (NodeStatus.DONE, NodeStatus.SKIPPED):
        return SessionStatus.DONE
    return SessionStatus.FAILED


__all__ = [
    "RunBlockReason",
    "RunDisposition",
    "evaluate_run",
    "session_status_for_node",
]
