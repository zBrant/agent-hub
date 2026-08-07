"""The status vocabularies are closed sets, shared with the UI and the harnesses.

These tests exist because the vocabularies are written down in three places —
`design.md` §5, `docs/design-system.md` §5, and ``app/harnesses/events.py`` — and
a status that only exists in two of them is a state the user can reach and the
interface cannot render.
"""

from typing import get_args

from app.harnesses.events import RunStatus
from app.harnesses.events import UsageSource as EventUsageSource
from app.models.status import NodeStatus, RunState, SessionStatus, UsageSource


def test_session_statuses_match_design() -> None:
    assert {s.value for s in SessionStatus} == {
        "planning",
        "running",
        "paused",
        "done",
        "failed",
    }


def test_node_statuses_match_the_design_system() -> None:
    # docs/design-system.md §5 assigns a colour token and an icon to each of
    # these eight. The frontend renders nothing for a ninth.
    assert [s.value for s in NodeStatus] == [
        "pending",
        "ready",
        "running",
        "awaiting_review",
        "blocked",
        "done",
        "failed",
        "skipped",
    ]


def test_run_state_is_the_harness_vocabulary_plus_running() -> None:
    """The one guard against the duplication in ``app/models/status.py``.

    ``RunState`` cannot import ``RunStatus`` — that would point a layer arrow
    upward and ``lint-imports`` would fail. This assertion is what keeps the two
    from drifting instead.
    """
    terminal = {state.value for state in RunState if state.terminal}
    assert terminal == set(get_args(RunStatus.__value__))
    assert {state.value for state in RunState} == terminal | {"running"}


def test_only_running_is_non_terminal() -> None:
    assert [state for state in RunState if not state.terminal] == [RunState.RUNNING]


def test_usage_source_matches_the_event_vocabulary() -> None:
    assert {source.value for source in UsageSource} == set(
        get_args(EventUsageSource.__value__)
    )
