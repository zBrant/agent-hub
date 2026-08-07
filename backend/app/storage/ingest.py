"""The write path for one run's events, in the one order that is allowed.

``docs/architecture.md`` §4:

1. append to ``events.ndjson``
2. update the SQLite projection
3. broadcast

Reverse any two of those and you have state in the database that does not exist
in the log: the dashboard shows a number nothing can explain, ``agenthub
replay`` disagrees with the row it rebuilt, and invariant 4 is fiction. Dying
*between* steps is fine and is the reason for the ordering — everything after
step 1 is reconstructible, so a crash costs you a projection, never a fact.

The module is split in two on purpose:

:class:`Projection`
    Step 2 alone. Applies one event to the repository, and nothing else.

:class:`RunIngest`
    Steps 1 → 2 → 3, with the broadcast injected.

Replay drives :class:`Projection` **directly** — literally the same code as live
ingest, with the log as the input instead of the output. That is what makes
"replay produces the same rows" a property of the design rather than of two
implementations agreeing today.

There is no broker yet (B6). Step 3 is a callback defaulting to
:func:`no_broadcast`, so B6 plugs in without moving anything: the only correct
place to publish an event is after it is durable in both stores.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import assert_never

from app.harnesses.base import ParseStats
from app.harnesses.events import (
    AgentEvent,
    AssistantText,
    Permission,
    RawChunk,
    RunFinished,
    RunStarted,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnFinished,
    TurnStarted,
    Usage,
)
from app.models.ids import RunId
from app.models.pricing import PriceTable
from app.storage.meta import RunMeta, meta_path, write_meta
from app.storage.ndjson import EventLog, events_path
from app.storage.repository import Repository

# Step 3. An awaitable so B6 can apply backpressure at the broker rather than
# here: ingest must not learn about subscriber queues.
Broadcast = Callable[[AgentEvent], Awaitable[None]]


async def no_broadcast(event: AgentEvent) -> None:
    """The default step 3: nobody is listening yet."""


class IngestError(Exception):
    """An event was fed to the wrong run, or through the wrong channel.

    Programmer error, not agent failure (`docs/architecture.md` §9).
    """


class Projection:
    """Step 2: one run's events applied to the SQLite projection.

    Stateful, and deliberately so — three of the derived columns are ordinals or
    running totals over the whole stream (``usage_event.seq``,
    ``run.event_count``, ``run.permission_denial_count``). Holding them here
    rather than querying for them keeps live ingest and replay on the same
    arithmetic, so a rebuilt row is equal rather than merely plausible.

    One instance per run, used once. Feeding a second pass over the same log
    into the same instance would continue the counters, which is why
    :func:`~app.storage.replay.replay_run` builds a fresh one after deleting the
    old rows.
    """

    def __init__(
        self, repository: Repository, run_id: RunId, *, prices: PriceTable
    ) -> None:
        self._repository = repository
        self._run_id = run_id
        # The table in effect for *this* run. Live ingest passes the current
        # one; replay passes the version pinned in meta.json (invariant 3).
        self._prices = prices
        self._events = 0
        self._usage_events = 0
        self._permission_denials = 0
        self._started = False
        self._finished = False

    @property
    def prices(self) -> PriceTable:
        return self._prices

    @property
    def events(self) -> int:
        """Events applied so far. Equals the line count of the log."""
        return self._events

    @property
    def usage_events(self) -> int:
        return self._usage_events

    @property
    def permission_denials(self) -> int:
        return self._permission_denials

    @property
    def started(self) -> bool:
        return self._started

    @property
    def finished(self) -> bool:
        """True once ``RunFinished`` has been applied.

        False at the end of a log means the process died without a terminal
        event — the run is ``interrupted``, and only an offline reader can say
        so (see :func:`~app.storage.replay.replay_run`).
        """
        return self._finished

    async def apply(self, event: AgentEvent) -> None:
        if event.run_id != self._run_id:
            raise IngestError(
                f"event for run {event.run_id} applied to run {self._run_id}"
            )
        # Counted before dispatch so RunFinished's own line is included in
        # run.event_count, which is the number of lines in events.ndjson.
        self._events += 1

        match event:
            case RunStarted():
                self._started = True
                await self._repository.start_run(self._run_id, event)
            case Usage():
                await self._repository.append_usage(
                    self._run_id,
                    event,
                    prices=self._prices,
                    seq=self._usage_events,
                )
                self._usage_events += 1
            case TurnFinished():
                # Accumulated rather than written per turn: `run` has no
                # per-turn row, and a refusal only changes a decision at the
                # end (B7 refuses to merge on it).
                self._permission_denials += len(event.permission_denials)
            case RunFinished():
                self._finished = True
                await self._repository.finish_run(
                    self._run_id,
                    event,
                    event_count=self._events,
                    permission_denial_count=self._permission_denials,
                )
            case RawChunk():
                # Channel B never enters Channel A's log (`docs/architecture.md`
                # §5): PTY bytes go to pty.log, are disposable, and would make
                # events.ndjson unbounded and unreplayable. Reaching here means
                # a caller crossed the channels.
                raise IngestError(
                    f"raw PTY chunk offered to the structural log of {self._run_id}; "
                    "Channel B is persisted to pty.log"
                )
            case (
                TurnStarted()
                | AssistantText()
                | ThinkingDelta()
                | ToolCall()
                | ToolResult()
                | Permission()
            ):
                # Narrative, not state. It lives in the log and streams to the
                # UI; projecting it would put prose in a relational table.
                pass
            case _:  # pragma: no cover - exhaustiveness, checked by mypy
                assert_never(event)


class RunIngest:
    """Steps 1 → 2 → 3 for one run.

    The ordering is the whole contract, so it exists exactly once, here, and
    every producer goes through :meth:`ingest`.
    """

    def __init__(
        self,
        *,
        log: EventLog,
        projection: Projection,
        broadcast: Broadcast = no_broadcast,
        meta: RunMeta,
        meta_file: Path,
    ) -> None:
        self._log = log
        self._projection = projection
        self._broadcast = broadcast
        self._meta = meta
        self._meta_file = meta_file

    @property
    def projection(self) -> Projection:
        return self._projection

    @property
    def meta(self) -> RunMeta:
        return self._meta

    @property
    def events(self) -> int:
        return self._log.count

    async def ingest(self, event: AgentEvent) -> None:
        """Durable first, derived second, visible third. Never another order."""
        await self._log.append(event)
        await self._projection.apply(event)
        await self._broadcast(event)

    async def finalize(self, *, at_ms: int, stats: ParseStats) -> RunMeta:
        """Write the run-end ``meta.json``: the parser's own verdict on itself.

        Called once the adapter's stream is exhausted. Until it is, the file on
        disk says ``finalized_ms: null`` and :attr:`RunMeta.trusted` is false —
        a run killed here is untrusted, which is the safe reading.
        """
        self._meta = self._meta.finalize(at_ms=at_ms, stats=stats)
        await write_meta(self._meta_file, self._meta)
        return self._meta


@asynccontextmanager
async def ingest_run(
    *,
    repository: Repository,
    runs_root: Path,
    meta: RunMeta,
    prices: PriceTable,
    broadcast: Broadcast = no_broadcast,
) -> AsyncIterator[RunIngest]:
    """Open a run's on-disk state and its projection together.

    Order of operations, and each one matters:

    1. the caller has already inserted the ``run`` row — it owns ``attempt``
       and ``created_ms``, and ``meta`` must carry the values *that row* got.
       Replay recreates the row from them, and a rebuilt row that disagrees
       about its attempt number or its creation time is not the same row
       (:func:`app.storage.meta.build_meta` takes both);
    2. ``meta.json`` is written **before** the first event, because it is what
       relinks a rebuilt run to its node if the process dies immediately;
    3. the log is opened, and everything after that goes through
       :meth:`RunIngest.ingest`.

    ``prices`` must be the table whose version is pinned in ``meta``. Passing a
    different one prices the run under a table it does not claim, which is the
    exact drift ``price_table_version`` exists to make detectable.
    """
    if prices.version != meta.price_table_version:
        raise IngestError(
            f"run {meta.run_id} pins price table version "
            f"{meta.price_table_version} but was handed version {prices.version}"
        )

    meta_file = meta_path(runs_root, meta.run_id)
    await write_meta(meta_file, meta)

    log = EventLog(events_path(runs_root, meta.run_id))
    await log.open()
    try:
        yield RunIngest(
            log=log,
            projection=Projection(repository, meta.run_id, prices=prices),
            broadcast=broadcast,
            meta=meta,
            meta_file=meta_file,
        )
    finally:
        await log.close()


__all__ = [
    "Broadcast",
    "IngestError",
    "Projection",
    "RunIngest",
    "ingest_run",
    "no_broadcast",
]
