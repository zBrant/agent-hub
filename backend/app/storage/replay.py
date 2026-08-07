"""Rebuild a run's projection from its log — invariant 4, executable.

``events.ndjson`` is the source of truth and SQLite is a derived index. That is
only a true statement if the index can actually be thrown away and rebuilt, so
this module does exactly that and ``agenthub replay <run_id>`` runs it.

Four rules it is written around.

**Only derived rows are discarded.** ``app/models/tables.py`` splits the schema
into *authored* input (``session`` and ``node``: what the user or planner asked
for, which no log can invent) and *derived* output (each column names the event
it comes from). Replay deletes the ``run`` row — cascading to its
``usage_event`` rows — and rebuilds it. It never deletes a ``session`` or a
``node``, and it refuses if the node is gone rather than inventing one.

**It re-projects through the live code.** :class:`~app.storage.ingest.Projection`
is step 2 of the write path and replay feeds it the log. There is no second
implementation to drift.

**It does not reprice history.** ``meta.json`` pins the ``price_table_version``
used at ingest and the pinned table is what prices the rebuild. If that version
is no longer in ``pricing.yaml``, replay **refuses and names it**
(:class:`~app.models.pricing.PriceTableNotFound`). Silently repricing at today's
prices would rewrite cost history and show up in no diff anywhere.

**Every mutation carries the event's own timestamp.** The repository takes
``at_ms`` on purpose (B2): a rebuilt row must equal the original, not be stamped
with the moment someone happened to run the rebuild.

What replay deliberately does **not** do is touch ``node.status`` or
``session.status``. Those are transitions, and `docs/architecture.md` §3 puts
every transition in ``orchestrator/graph.py:transition`` — which lives *above*
storage and may not be imported from here. :attr:`ReplayResult.run_status` is
handed back so the orchestrator can apply it; deciding it here would be a second
copy of the rule that already caused one bug in every system that tried it.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.harnesses.events import AgentEvent, agent_event_adapter
from app.models.ids import NodeId, RunId, SessionId
from app.models.pricing import PriceHistory
from app.models.status import RunState
from app.storage.ingest import Projection
from app.storage.meta import RunMeta, meta_path, read_meta
from app.storage.ndjson import events_path
from app.storage.repository import Repository, UsageTotals

# What a truncated tail is recorded as, so the run's summary explains itself in
# the UI instead of just reading `interrupted`.
TRUNCATED_SUMMARY = "log truncated: the process died mid-write"
UNFINISHED_SUMMARY = "log ends with no run_finished event"


class ReplayError(Exception):
    """The log, the metadata, or the rows above them cannot support a rebuild."""


@dataclass(frozen=True, slots=True)
class LogRead:
    """The events recovered from a log, plus what was wrong with the tail."""

    events: tuple[AgentEvent, ...]
    # 1-based line number of a final, partially written line, if there was one.
    truncated_line: int | None = None

    @property
    def truncated(self) -> bool:
        return self.truncated_line is not None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    """What the rebuild produced. Everything a caller needs to verify it."""

    run_id: RunId
    node_id: NodeId
    session_id: SessionId
    price_table_version: int
    events: int
    usage_events: int
    totals: UsageTotals
    # The state the log says the run ended in. Applying it upward to the node is
    # the orchestrator's call, not storage's — see the module docstring.
    run_status: RunState
    truncated_line: int | None
    permission_denials: int
    # meta.json's single trust predicate, carried through so a caller does not
    # have to re-read the file to know whether the run may be merged.
    trusted: bool

    @property
    def truncated(self) -> bool:
        return self.truncated_line is not None


def read_log(path: Path) -> LogRead:
    """Read a run's log, tolerating a torn final line and nothing else.

    **Policy: recover up to the truncation, and mark the run interrupted.**

    A partial final line is the ordinary signature of SIGKILL between the
    write and the flush of one record — :class:`~app.storage.ndjson.EventLog`
    flushes per line, so everything before it is a complete, durable fact.
    Refusing the whole log would discard hundreds of real events to protest
    about the last forty bytes, and it would make replay useless in exactly the
    case it exists for: recovering a run that died.

    A bad line anywhere *else*, or a bad final line in a file that ends with a
    newline, is a different thing. The writer completed that record, so
    something else damaged it. That is corruption, not a crash, and it raises —
    silently skipping it would drop tokens out of the totals with no trace.

    Synchronous, like :func:`app.storage.ndjson.read_events`: replay is an
    offline operation. Callers on the event loop wrap it in
    :func:`asyncio.to_thread` (invariant 5).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReplayError(f"cannot read event log at {path}: {exc}") from exc

    lines = text.splitlines()
    complete = text.endswith("\n")
    events: list[AgentEvent] = []
    truncated_line: int | None = None

    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            events.append(agent_event_adapter.validate_python(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as exc:
            if lineno == len(lines) and not complete:
                truncated_line = lineno
                break
            raise ReplayError(
                f"{path}:{lineno} does not parse and is not the torn tail of a "
                f"killed write; the log is corrupt: {exc}"
            ) from exc

    return LogRead(events=tuple(events), truncated_line=truncated_line)


async def replay_run(
    *,
    repository: Repository,
    runs_root: Path,
    run_id: RunId,
    prices: PriceHistory,
) -> ReplayResult:
    """Discard and rebuild the derived rows of one run from its log.

    Idempotent: replaying twice produces the same rows, the same ``seq`` values
    and the same totals. The ``run`` row is deleted first (cascading to
    ``usage_event``) so a second pass cannot double a total —
    ``uq_usage_event_run_id_seq`` would raise rather than let it, which is why
    B2 put it there.

    Offline by contract. Replaying a run whose process is still alive will mark
    it interrupted, because from the log's point of view it is: there is no
    ``run_finished`` in it yet.
    """
    meta = await _load_meta(runs_root, run_id)
    # Refuse before touching the database: a rebuild that stops halfway is
    # worse than one that never started.
    table = prices.table(meta.price_table_version)

    node = await repository.get_node(meta.node_id)
    if node is None:
        raise ReplayError(
            f"run {run_id} belongs to node {meta.node_id}, which does not exist. "
            "Replay rebuilds derived rows only; a node is authored input and "
            "cannot be reconstructed from a log."
        )
    if node.session_id != meta.session_id:
        raise ReplayError(
            f"meta.json puts run {run_id} in session {meta.session_id} but node "
            f"{meta.node_id} belongs to {node.session_id}"
        )

    log_path = events_path(runs_root, run_id)
    log = await asyncio.to_thread(read_log, log_path)

    await repository.delete_run(run_id)
    await repository.create_run(
        run_id=run_id,
        node_id=meta.node_id,
        events_path=log_path,
        harness=meta.harness,
        model=meta.model,
        # From meta.json, never reallocated: the attempt number is history, and
        # `next_attempt()` would hand out a different one now that the row is
        # gone (`uq_run_node_id_attempt`).
        attempt=meta.attempt,
        at_ms=meta.created_ms,
    )

    projection = Projection(repository, run_id, prices=table)
    for event in log.events:
        await projection.apply(event)

    if not projection.finished:
        # No terminal event. Live ingest would have left the row `running`
        # because the process was still, as far as it knew, alive; an offline
        # reader knows better, and B2 named this exact case on
        # `mark_run_interrupted`.
        await repository.mark_run_interrupted(
            run_id,
            at_ms=_last_ts(log, meta),
            summary=TRUNCATED_SUMMARY if log.truncated else UNFINISHED_SUMMARY,
            event_count=projection.events,
            permission_denial_count=projection.permission_denials,
        )

    row = await repository.get_run(run_id)
    if row is None:  # pragma: no cover - we just wrote it
        raise ReplayError(f"run {run_id} vanished during replay")

    return ReplayResult(
        run_id=run_id,
        node_id=meta.node_id,
        session_id=meta.session_id,
        price_table_version=meta.price_table_version,
        events=projection.events,
        usage_events=projection.usage_events,
        totals=await repository.usage_totals(run_id=run_id),
        run_status=row.status,
        truncated_line=log.truncated_line,
        permission_denials=projection.permission_denials,
        trusted=meta.trusted,
    )


async def _load_meta(runs_root: Path, run_id: RunId) -> RunMeta:
    meta = await read_meta(meta_path(runs_root, run_id))
    if meta.run_id != run_id:
        raise ReplayError(
            f"{meta_path(runs_root, run_id)} describes run {meta.run_id}, not {run_id}"
        )
    return meta


def _last_ts(log: LogRead, meta: RunMeta) -> int:
    """When the run stopped, as far as anything on disk can say.

    The last event's stamp, or the run's creation time for a log that never got
    a single line out. Never ``now_ms()``: a row rebuilt today must not claim
    the run ended today.
    """
    return log.events[-1].ts if log.events else meta.created_ms


__all__ = [
    "LogRead",
    "ReplayError",
    "ReplayResult",
    "read_log",
    "replay_run",
]
