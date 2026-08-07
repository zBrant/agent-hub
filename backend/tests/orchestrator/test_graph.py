"""Pure Phase 1 lifecycle decisions."""

import pytest

from app.models.status import NodeStatus, RunState, SessionStatus
from app.orchestrator.graph import (
    RunBlockReason,
    evaluate_run,
    session_status_for_node,
)


def test_only_a_trusted_successful_changed_run_is_mergeable() -> None:
    disposition = evaluate_run(
        RunState.SUCCESS,
        trusted=True,
        permission_denials=0,
        changed=True,
    )
    assert disposition.mergeable is True
    assert disposition.node_status is NodeStatus.AWAITING_REVIEW
    assert disposition.reason is None


@pytest.mark.parametrize(
    ("trusted", "denials", "changed", "reason"),
    [
        (False, 0, True, RunBlockReason.PARSER_UNTRUSTED),
        (True, 1, True, RunBlockReason.PERMISSION_DENIED),
        (True, 0, False, RunBlockReason.NO_CHANGES),
    ],
)
def test_a_successful_but_unsafe_run_is_blocked(
    trusted: bool,
    denials: int,
    changed: bool,
    reason: RunBlockReason,
) -> None:
    disposition = evaluate_run(
        RunState.SUCCESS,
        trusted=trusted,
        permission_denials=denials,
        changed=changed,
    )
    assert disposition.mergeable is False
    assert disposition.node_status is NodeStatus.BLOCKED
    assert disposition.reason is reason


@pytest.mark.parametrize(
    "status",
    [RunState.FAILED, RunState.INTERRUPTED, RunState.BUDGET_EXCEEDED],
)
def test_an_unsuccessful_run_fails_the_node(status: RunState) -> None:
    disposition = evaluate_run(
        status,
        trusted=True,
        permission_denials=0,
        changed=True,
    )
    assert disposition.mergeable is False
    assert disposition.node_status is NodeStatus.FAILED


@pytest.mark.parametrize(
    ("node", "session"),
    [
        (NodeStatus.PENDING, SessionStatus.PLANNING),
        (NodeStatus.READY, SessionStatus.PLANNING),
        (NodeStatus.RUNNING, SessionStatus.RUNNING),
        (NodeStatus.AWAITING_REVIEW, SessionStatus.PAUSED),
        (NodeStatus.BLOCKED, SessionStatus.PAUSED),
        (NodeStatus.DONE, SessionStatus.DONE),
        (NodeStatus.SKIPPED, SessionStatus.DONE),
        (NodeStatus.FAILED, SessionStatus.FAILED),
    ],
)
def test_single_node_projects_the_session_status(
    node: NodeStatus, session: SessionStatus
) -> None:
    assert session_status_for_node(node) is session
