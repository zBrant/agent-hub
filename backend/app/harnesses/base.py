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
from typing import Protocol, runtime_checkable

from app.harnesses.events import AgentEvent, Usage
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
    model: str | None = None
    cwd: Path | None = None
    interrupted: bool = False
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
    # The harness's direct per-turn usage was zero and had to be reconstructed
    # from a second accounting it publishes.
    zero_usage_turns: int = 0
    # ...and the reconstruction itself was not trustworthy, so no Usage was
    # emitted for that turn. Non-zero means tokens are missing from the totals.
    usage_unreconciled_turns: int = 0

    @property
    def unhandled(self) -> int:
        """Lines the parser did not understand. Non-zero means investigate."""
        return sum(self.unknown.values()) + self.malformed

    def count_ignored(self, key: str) -> None:
        self.ignored[key] = self.ignored.get(key, 0) + 1

    def count_unknown(self, key: str) -> None:
        self.unknown[key] = self.unknown.get(key, 0) + 1


@dataclass(frozen=True)
class StructuredRequest:
    """One shot at a schema-constrained answer. No tools, no worktree, no run.

    Deliberately not a :class:`RunSpec`. A run is a node of the graph: it has a
    ``run_id``, a worktree, an NDJSON log, and rows in SQLite. This is a
    question with a shape, used by the planner *before* any of that exists, and
    giving it a ``run_id`` would put a row in ``usage_event`` for something that
    is not a node (`design.md` §8's fourth consequence).

    ``schema`` is a JSON Schema object in the **strict, resolved** dialect:

    * no ``$ref`` and no ``$defs`` — Pydantic emits both by default;
    * every object carries ``additionalProperties: false`` and a ``required``
      listing every key in ``properties``.

    Both are the caller's job rather than each adapter's. Codex rejects a
    schema that misses either (``invalid_json_schema``, exit 1) while Claude
    Code accepts the loose form too, so the strict dialect is the portable one
    — and normalizing once at the caller keeps that out of the adapters, where
    it would become a per-harness quirk. An adapter may assume this and does
    not validate it.
    """

    prompt: str
    schema: Mapping[str, object]
    system: str | None = None
    model: str | None = None
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    launcher: tuple[str, ...] = ()


@dataclass(frozen=True)
class StructuredResult:
    """What came back, plus what it cost.

    ``data`` has been validated against the schema *by the harness*, so a
    caller still has to validate it against its own model — the CLIs enforce
    shape, not the caller's Pydantic semantics.

    ``usage`` is the same four fields as everything else (invariant 3), and it
    is an estimated equivalent rather than spend whenever the harness runs
    under a subscription (invariant 7). That is the whole point of this path.
    """

    data: Mapping[str, object]
    usage: Usage | None
    model: str


@runtime_checkable
class StructuredCompleter(Protocol):
    """The optional half of the adapter contract: schema-constrained answers.

    Separate from :class:`BaseHarnessAdapter` because it is genuinely optional.
    Claude Code has ``--json-schema`` and Codex has ``--output-schema``, but a
    future adapter may have neither, and the honest way to say so is to not
    implement this — a stub raising "unsupported" would make every caller check
    at runtime anyway, and this way mypy checks it.

    Callers ask :func:`supports_structured_output`, which is a *capability*
    question and not a branch on which harness is running: invariant 1 forbids
    the second, not the first.
    """

    async def complete_structured(
        self, request: StructuredRequest
    ) -> StructuredResult: ...


class BaseHarnessAdapter(Protocol):
    """`design.md` §3. Implementations live one file per harness in this package."""

    name: str
    supported_models: list[str]
    stats: ParseStats

    def build_argv(self, spec: RunSpec) -> list[str]: ...

    async def start(self, spec: RunSpec) -> RunHandle: ...

    async def send(self, handle: RunHandle, text: str) -> None: ...

    async def interrupt(self, handle: RunHandle) -> None: ...

    async def kill(self, handle: RunHandle) -> None: ...

    def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]: ...


def supports_structured_output(adapter: BaseHarnessAdapter) -> bool:
    """Whether `adapter` can answer a :class:`StructuredRequest`.

    A capability question, which invariant 1 permits, and not ``adapter.name ==
    "claude-code"``, which it forbids.
    """
    return isinstance(adapter, StructuredCompleter)
