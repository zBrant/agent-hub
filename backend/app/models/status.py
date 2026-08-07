"""The status vocabularies of `design.md` §5.

Three closed sets, and they are the same three the UI already assumes
(`docs/design-system.md` §5 maps every node state to a colour token and an
icon). A status that exists here and not there renders as nothing; a status
that exists there and not here is unreachable. Adding one is a change to both
documents plus a migration, never a new string literal at a call site.

**Why the run vocabulary is redeclared here.** The canonical terminal statuses
live in :data:`app.harnesses.events.RunStatus`, and that is the right home for
them — a harness reports how a run ended. But `docs/architecture.md` §1 puts
``models/`` *below* ``harnesses/``, so importing them down here would reverse a
layer arrow and ``lint-imports`` would (correctly) fail the build. The
duplication is therefore deliberate and one-directional: :class:`RunState` is
``RunStatus`` plus the one non-terminal state a persisted row needs, and
``tests/models/test_status.py`` fails the moment the two drift apart. The clean
fix is to move ``RunStatus``/``UsageSource`` into this module and re-export them
from ``harnesses/events.py``; that edits a file B2 may not touch.
"""

from enum import StrEnum


class SessionStatus(StrEnum):
    """One planning conversation plus its graph."""

    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"


class NodeStatus(StrEnum):
    """One activity in the graph.

    ``blocked`` is not a failure: it is a merge conflict or a permission gate
    waiting on a human, and it is reachable again. ``failed`` is the run's
    verdict. They are separate states because the operator's next action
    differs, which is also why `docs/design-system.md` §5 distinguishes them by
    icon rather than by hue.
    """

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class RunState(StrEnum):
    """One execution of a node.

    ``RUNNING`` is the only non-terminal member and the only one with no
    counterpart in ``RunStatus``: a harness never *reports* "still running", it
    is the state of a row between :class:`~app.harnesses.events.RunStarted` and
    :class:`~app.harnesses.events.RunFinished`. A row still ``RUNNING`` after an
    orchestrator restart is an orphan, and the scheduler resolves it to
    ``INTERRUPTED`` rather than to a sixth state.
    """

    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    BUDGET_EXCEEDED = "budget_exceeded"

    @property
    def terminal(self) -> bool:
        return self is not RunState.RUNNING


class UsageSource(StrEnum):
    """Where a usage row's numbers came from.

    ``RECONSTRUCTED`` means the adapter derived them from a second accounting
    the harness publishes because the direct one was missing or zeroed (see
    ``app/harnesses/events.py:Usage``). It is stored rather than dropped so a
    dashboard can say so and a reconciliation bug stays attributable.
    """

    REPORTED = "reported"
    RECONSTRUCTED = "reconstructed"
