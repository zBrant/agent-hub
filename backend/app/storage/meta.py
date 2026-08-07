"""``runs/<run_id>/meta.json`` — what the event log structurally cannot carry.

An :class:`~app.harnesses.events.AgentEvent` describes what the *agent* did.
Three kinds of fact belong to the *orchestration* instead, and no harness will
ever emit them (`docs/architecture.md` §4):

``session_id`` / ``node_id`` / ``attempt``
    ``RunStarted`` carries the **harness's** session id, not ours. A run row
    deleted for a rebuild has to be relinked to the node that authored it, and
    the log contains no edge back up the graph.

``price_table_version``
    Pinned at run start so replay prices with the table that was in effect then
    instead of today's (invariant 3, `design.md` §4).

``argv`` / ``cwd`` / sanitized ``env`` / harness and version
    The launch conditions. Reproducing a run means reproducing these.

``parser``
    :class:`~app.harnesses.base.ParseStats` is adapter state, not an event. B7
    must refuse to merge a parser-untrusted run and B9 must display it, so the
    counters have to outlive the process that produced them.

**This file is part of the source of truth, not a projection.** Deleting it
loses information ``events.ndjson`` cannot reconstruct — which is why
:func:`write_meta` replaces it atomically and never truncates in place.

It is written once at run start and finalized at run end. A run killed in
between leaves a readable, obviously-unfinalized file: :attr:`RunMeta.finalized`
is false, :attr:`RunMeta.trusted` is false, and nothing downstream may treat the
absence of parser counters as "no problems found".

The environment is recorded through an **allowlist** (`docs/conventions.md` §6).
A denylist is a promise to have predicted every name a credential can have, and
``meta.json`` sits in a directory a user will paste into a bug report.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.harnesses.base import ParseStats
from app.models.ids import NodeId, RunId, SessionId
from app.storage.ndjson import run_dir

META_FILENAME = "meta.json"

# The only environment variables copied into meta.json. Exact names, no
# prefixes and no patterns: `AGENT_*` would have looked harmless right up to the
# first `AGENT_API_TOKEN`. Every entry here is a launch condition that changes
# how a process behaves, and none of them can hold a credential.
ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "NO_COLOR",
        "PATH",
        "PWD",
        "SHELL",
        "TERM",
        "TMPDIR",
        "TZ",
        "USER",
    }
)


class MetaError(Exception):
    """``meta.json`` is missing, unreadable, or not the shape we wrote."""


def meta_path(runs_root: Path, run_id: RunId) -> Path:
    """Beside ``events.ndjson``: one directory holds a run's whole truth."""
    return run_dir(runs_root, run_id) / META_FILENAME


def sanitize_env(env: Mapping[str, str]) -> dict[str, str]:
    """Keep only :data:`ENV_ALLOWLIST`. Everything else never reaches disk."""
    return {key: env[key] for key in sorted(ENV_ALLOWLIST & env.keys())}


class ParserTrust(BaseModel):
    """The durable form of :class:`~app.harnesses.base.ParseStats`.

    Storage owns this shape rather than serializing the adapter's dataclass:
    ``ParseStats`` is free to grow a field for a parser's own bookkeeping, and
    the on-disk schema of a source-of-truth file should not move with it.

    :attr:`trusted` is the single predicate consumers use. B7 (refuse to merge)
    and B9 (show it) must not each re-derive trust from the counters — two
    derivations are two chances to disagree about whether a diff is safe.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: int = 0
    events: int = 0
    ignored: dict[str, int] = Field(default_factory=dict)
    # Line shapes the parser did not recognize, keyed by whatever the adapter
    # calls them. A new line type in the next CLI release lands here.
    unknown: dict[str, int] = Field(default_factory=dict)
    malformed: int = 0
    # The harness reported zero tokens for a turn and the adapter rebuilt them
    # from a second accounting. Not itself a trust problem: the reconstruction
    # is validated, and the resulting Usage says `source="reconstructed"`.
    zero_usage_turns: int = 0
    # ...and here the reconstruction failed, so tokens are missing from the
    # totals. Cost for this run is understated by an unknown amount.
    usage_unreconciled_turns: int = 0

    @classmethod
    def from_stats(cls, stats: ParseStats) -> Self:
        return cls(
            lines=stats.lines,
            events=stats.events,
            ignored=dict(stats.ignored),
            unknown=dict(stats.unknown),
            malformed=stats.malformed,
            zero_usage_turns=stats.zero_usage_turns,
            usage_unreconciled_turns=stats.usage_unreconciled_turns,
        )

    @property
    def unhandled(self) -> int:
        """Lines the parser did not understand at all."""
        return sum(self.unknown.values()) + self.malformed

    @property
    def trusted(self) -> bool:
        """True when this run's stream was fully understood.

        False means either the parser met something it could not read, or it
        knows tokens are missing. Both make the run's own report of itself
        unreliable, which is a merge decision, not a cosmetic one.
        """
        return self.unhandled == 0 and self.usage_unreconciled_turns == 0


class RunMeta(BaseModel):
    """The orchestration facts about one run.

    Frozen: a fact recorded at launch is not editable afterwards. Finalizing
    produces a *new* object (:meth:`finalize`) that is written over the old file
    atomically, so a kill during the write leaves the start version intact
    rather than a half-file.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Bumped when a field changes meaning. Old runs must keep loading, so new
    # fields are optional with a default (`docs/conventions.md` §2).
    schema_version: int = 1

    run_id: RunId
    session_id: SessionId
    node_id: NodeId
    # The run's ordinal among its node's attempts. Replay recreates the deleted
    # run row with this number; allocating a fresh one would rewrite history.
    attempt: int = Field(ge=1)

    # Pinned here, not read from the current file at replay time. See the
    # module docstring and invariant 3.
    price_table_version: int

    # Data, never a conditional (invariant 1).
    harness: str
    harness_version: str | None = None
    model: str | None = None

    # The launch conditions. `argv` is the sandboxed argv actually executed —
    # never the prompt, which travels on stdin precisely because argv is visible
    # in `ps` (`docs/conventions.md` §6).
    argv: tuple[str, ...] = ()
    cwd: Path
    env: dict[str, str] = Field(default_factory=dict)

    created_ms: int
    # None until the run ends. This is the "obviously unfinalized" signal.
    finalized_ms: int | None = None
    parser: ParserTrust | None = None

    @property
    def finalized(self) -> bool:
        return self.finalized_ms is not None

    @property
    def trusted(self) -> bool:
        """The one predicate for "may this run's output be believed".

        An unfinalized run is **not** trusted: the process died before the
        adapter could report what it had failed to parse, so the absence of
        counters says nothing. Treating unknown as clean is how an untrusted
        diff gets merged.
        """
        return self.parser is not None and self.parser.trusted

    def finalize(
        self,
        *,
        at_ms: int,
        stats: ParseStats,
        harness_version: str | None = None,
    ) -> RunMeta:
        """The run-end version of this file."""
        update: dict[str, object] = {
            "finalized_ms": at_ms,
            "parser": ParserTrust.from_stats(stats),
        }
        # Most CLIs only reveal their exact version in the first stream event.
        # The start copy remains useful if the process dies before that event;
        # the finalized copy records the observed version when it exists.
        if harness_version is not None:
            update["harness_version"] = harness_version
        return self.model_copy(update=update)


def write_meta_sync(path: Path, meta: RunMeta) -> None:
    """Write ``meta.json`` atomically. Blocking; prefer :func:`write_meta`.

    Temp file plus ``rename`` because this is source of truth: a SIGKILL during
    the finalizing write must leave the run-start version readable, not a
    truncated one. ``rename`` within a directory is atomic on APFS and ext4.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.tmp")
    temp.write_text(
        json.dumps(meta.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


async def write_meta(path: Path, meta: RunMeta) -> None:
    """Write ``meta.json`` off the event loop (invariant 5)."""
    await asyncio.to_thread(write_meta_sync, path, meta)


def read_meta_sync(path: Path) -> RunMeta:
    """Read ``meta.json``. Blocking; callers on the loop use :func:`read_meta`."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MetaError(f"cannot read run metadata at {path}: {exc}") from exc
    try:
        return RunMeta.model_validate_json(raw)
    except ValidationError as exc:
        raise MetaError(f"{path} is not valid run metadata: {exc}") from exc


async def read_meta(path: Path) -> RunMeta:
    """Read ``meta.json`` off the event loop (invariant 5)."""
    return await asyncio.to_thread(read_meta_sync, path)


def build_meta(
    *,
    run_id: RunId,
    session_id: SessionId,
    node_id: NodeId,
    attempt: int,
    price_table_version: int,
    harness: str,
    cwd: Path,
    created_ms: int,
    argv: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    harness_version: str | None = None,
    model: str | None = None,
) -> RunMeta:
    """A run-start :class:`RunMeta` with the environment already sanitized.

    The one constructor callers should use: it is the only place that guarantees
    :func:`sanitize_env` ran, so no caller can forget.
    """
    return RunMeta(
        run_id=run_id,
        session_id=session_id,
        node_id=node_id,
        attempt=attempt,
        price_table_version=price_table_version,
        harness=harness,
        harness_version=harness_version,
        model=model,
        argv=tuple(argv),
        cwd=cwd,
        env=sanitize_env(env or {}),
        created_ms=created_ms,
    )


__all__ = [
    "ENV_ALLOWLIST",
    "META_FILENAME",
    "MetaError",
    "ParserTrust",
    "RunMeta",
    "build_meta",
    "meta_path",
    "read_meta",
    "read_meta_sync",
    "sanitize_env",
    "write_meta",
    "write_meta_sync",
]
