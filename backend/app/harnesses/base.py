"""The adapter contract (`design.md` §3).

Every harness implements :class:`BaseHarnessAdapter` and produces
:data:`~app.harnesses.events.AgentEvent`. That is the whole public surface of
this package — nothing above it may branch on which CLI is running
(invariant 1).

Why :class:`RunSpec` carries a ``launcher`` instead of a sandbox policy: the
import-linter contract puts ``app.harnesses`` and ``app.sandbox`` in the *same*
layer, which makes them independent — a harness may not import the sandbox and
vice versa. The orchestrator builds the ai-jail argv
(:func:`app.sandbox.aijail.build_argv`) and hands the prefix to the adapter,
which appends its own. Neither module learns about the other, and invariant 8
still holds because the argv the adapter execs is the sandboxed one.
"""

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.harnesses.events import AgentEvent
from app.models.ids import RunId

# Kept from the child's stderr for diagnostics when a launch fails. stderr is
# never parsed and never merged into stdout: Channel A has to stay clean JSON,
# and ai-jail prints its banner there (phase-0 A1 finding).
STDERR_TAIL_LINES = 50


class HarnessError(Exception):
    """A harness could not be driven — bad launch, broken pipe, missing binary.

    Not for *the agent failed*: that is a ``RunFinished(status=...)`` event
    (`docs/architecture.md` §9). Reparent under ``AgentHubError`` once that base
    exists (`docs/conventions.md` §2).
    """


@dataclass(frozen=True)
class RunSpec:
    """Everything needed to launch one node's agent.

    ``prompt`` is passed to the child on **stdin**, never in argv: argv is
    visible in ``ps`` and a prompt is untrusted, potentially sensitive content
    (`docs/conventions.md` §6).
    """

    run_id: RunId
    cwd: Path
    prompt: str
    model: str | None = None
    # Explicit child environment; empty means inherit ours.
    env: Mapping[str, str] = field(default_factory=dict)
    # Sandbox argv prefix, e.g. ("ai-jail", "--clean", ...). Empty runs the CLI
    # unsandboxed, which is only ever acceptable in a test.
    launcher: tuple[str, ...] = ()
    max_budget_usd: float | None = None


@dataclass
class RunHandle:
    """A launched harness process.

    Concrete rather than a Protocol on purpose: all three MVP harnesses are
    child processes. OpenCode's server mode will need a second shape, and that
    is the moment to generalize — not before.
    """

    run_id: RunId
    argv: tuple[str, ...]
    process: asyncio.subprocess.Process
    started_ms: int
    stderr_tail: deque[str] = field(
        default_factory=lambda: deque(maxlen=STDERR_TAIL_LINES)
    )
    # Background readers (stderr drain, stdin writer) owned by the adapter.
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    @property
    def pid(self) -> int:
        return self.process.pid


@dataclass
class ParseStats:
    """What a stream translation did and, more importantly, did *not* do.

    An unrecognized line must never be silently discarded: a new line type in
    the next CLI release is exactly the failure this package exists to catch.
    ``ignored`` counts shapes we drop **on purpose** (each one justified in the
    adapter's ``IGNORED_LINES`` table); ``unknown`` and ``malformed`` count the
    ones we did not expect, and the adapter also logs those at warning level.
    """

    lines: int = 0
    events: int = 0
    ignored: dict[str, int] = field(default_factory=dict)
    unknown: dict[str, int] = field(default_factory=dict)
    malformed: int = 0
    # result.usage reported all zeros while the harness reported real spend.
    zero_usage_turns: int = 0

    @property
    def unhandled(self) -> int:
        """Lines the parser did not understand. Non-zero means investigate."""
        return sum(self.unknown.values()) + self.malformed

    def count_ignored(self, key: str) -> None:
        self.ignored[key] = self.ignored.get(key, 0) + 1

    def count_unknown(self, key: str) -> None:
        self.unknown[key] = self.unknown.get(key, 0) + 1


class BaseHarnessAdapter(Protocol):
    """`design.md` §3. Implementations live one file per harness in this package."""

    name: str
    supported_models: list[str]

    async def start(self, spec: RunSpec) -> RunHandle: ...

    async def send(self, handle: RunHandle, text: str) -> None: ...

    async def interrupt(self, handle: RunHandle) -> None: ...

    async def kill(self, handle: RunHandle) -> None: ...

    def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]: ...
