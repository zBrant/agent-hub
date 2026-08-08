"""Bounded in-memory fan-out for durable ``AgentEvent`` facts.

The broker is deliberately not persistence. NDJSON and SQLite are written
before :meth:`publish` is called; this module only bridges that durable state to
live clients. A slow client is disconnected instead of ever delaying ingest.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from app.harnesses.events import AgentEvent, agent_event_adapter
from app.metrics.system import SystemSnapshot
from app.models.ids import RunId, SessionId

type WireMessage = dict[str, object]


class InvalidTopicError(ValueError):
    """A client requested a topic outside the public vocabulary."""


class ReplayGapError(Exception):
    """The requested cursor is older than the bounded replay window."""


@dataclass(frozen=True, slots=True)
class _EventFrame:
    topic: str
    seq: int
    payload: dict[str, object]
    #: The wire ``type`` this frame is delivered under. ``event`` carries an
    #: :class:`~app.harnesses.events.AgentEvent`; ``node_status`` carries a
    #: graph transition, which is orchestration state and not harness output.
    #: They share the envelope — same ``stream``, same per-topic ``seq``, same
    #: replay path — so a reconnect resumes a graph topic exactly the way it
    #: resumes a run topic, and a client can discriminate on one field.
    kind: str = "event"


@dataclass(frozen=True, slots=True)
class _Closed:
    pass


_CLOSED = _Closed()


@dataclass(eq=False, slots=True)
class BrokerConnection:
    """One browser connection and its bounded outbound queue."""

    queue: asyncio.Queue[WireMessage | _Closed]
    topics: set[str] = field(default_factory=set)
    closed: bool = False

    async def receive(self) -> WireMessage | None:
        """Return the next frame, or ``None`` after broker-side disconnect."""
        message = await self.queue.get()
        return None if isinstance(message, _Closed) else message


class EventBroker:
    """Multiplex run and session topics without backpressuring event ingest."""

    def __init__(
        self,
        *,
        queue_size: int = 256,
        history_size: int = 256,
        max_topics: int = 128,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if history_size < 1:
            raise ValueError("history_size must be positive")
        if max_topics < 1:
            raise ValueError("max_topics must be positive")
        self.stream_id = uuid4().hex
        self._queue_size = queue_size
        self._history_size = history_size
        self._max_topics = max_topics
        self._connections: set[BrokerConnection] = set()
        # Ordered by least-recently-published. A graph session opens one
        # `run:<id>` topic per node and never closes it, so without a bound the
        # retained frames grow with every node the orchestrator has ever run.
        self._history: OrderedDict[str, deque[_EventFrame]] = OrderedDict()
        self._sequences: dict[str, int] = defaultdict(int)
        # Highest sequence per topic that is no longer replayable, whether it
        # aged out of a full deque or the whole topic was evicted. This is what
        # keeps an eviction *detectable*: without it, a topic whose history was
        # dropped looks identical to one that never published, and a client
        # reconnecting with an old cursor would be told it is up to date and
        # silently skip everything in between.
        self._dropped_through: dict[str, int] = defaultdict(int)
        self._run_sessions: dict[RunId, SessionId] = {}
        # Metrics are ephemeral snapshots, not durable facts. Retain exactly
        # one so a new subscriber hydrates immediately without replaying five
        # minutes of one-second samples.
        self._latest_metrics: _EventFrame | None = None
        self._lock = asyncio.Lock()

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def register_run(self, run_id: RunId, session_id: SessionId) -> None:
        """Link a run before its first event can reach the broker."""
        async with self._lock:
            self._run_sessions[run_id] = session_id

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[BrokerConnection]:
        connection = BrokerConnection(asyncio.Queue(maxsize=self._queue_size))
        async with self._lock:
            self._connections.add(connection)
        try:
            yield connection
        finally:
            await self.disconnect(connection)

    async def disconnect(self, connection: BrokerConnection) -> None:
        async with self._lock:
            self._disconnect_locked(connection)

    async def subscribe(
        self,
        connection: BrokerConnection,
        topic: str,
        *,
        stream: str | None = None,
        after: int | None = None,
    ) -> None:
        """Atomically attach live delivery and, for a valid cursor, replay.

        A missing or foreign stream starts at the current edge and emits a
        ``ready`` checkpoint. A matching stream replays every retained frame
        after ``after`` before any newly published frame can be enqueued.
        """
        self._validate_topic(topic)
        if (stream is None) != (after is None):
            raise ValueError("stream and after must be provided together")
        if after is not None and after < 0:
            raise ValueError("after must be non-negative")

        async with self._lock:
            if connection.closed:
                return
            if topic == "metrics" and self._latest_metrics is not None:
                connection.topics.add(topic)
                self._put_locked(connection, self._wire_event(self._latest_metrics))
                return
            current = self._sequences[topic]
            if stream != self.stream_id or after is None or after > current:
                connection.topics.add(topic)
                self._put_locked(
                    connection,
                    {
                        "type": "ready",
                        "stream": self.stream_id,
                        "topic": topic,
                        "cursor": current,
                    },
                )
                return

            dropped = self._dropped_through[topic]
            if after < dropped:
                raise ReplayGapError(
                    f"cursor {after} precedes retained history; "
                    f"frames through {dropped} are no longer replayable"
                )
            connection.topics.add(topic)
            for frame in self._history.get(topic, ()):
                if frame.seq > after:
                    if not self._put_locked(connection, self._wire_event(frame)):
                        return

    async def unsubscribe(self, connection: BrokerConnection, topic: str) -> None:
        self._validate_topic(topic)
        async with self._lock:
            connection.topics.discard(topic)

    async def notify(self, connection: BrokerConnection, message: WireMessage) -> None:
        """Queue a control response through the connection's sole writer."""
        async with self._lock:
            self._put_locked(connection, message)

    async def publish(self, event: AgentEvent) -> None:
        """Fan out one already-durable event; never wait for a subscriber."""
        payload = agent_event_adapter.dump_python(event, mode="json")
        topics = [f"run:{event.run_id}"]
        async with self._lock:
            session_id = self._run_sessions.get(event.run_id)
            if session_id is not None:
                topics.append(f"session:{session_id}")

            for topic in topics:
                self._fan_out_locked(topic, payload)

    async def publish_node_status(
        self, *, session_id: str, node_id: str, status: str, ts: int
    ) -> None:
        """Fan out one already-persisted node transition on ``graph:<session>``.

        The graph topic is `docs/phase-2.md` C9's: a canvas needs to know that
        node *b* went ``awaiting_review`` without subscribing to every run of
        every node, and node status is not an ``AgentEvent`` — no harness emits
        it, and inventing a variant for it would put orchestration state into
        the union `docs/architecture.md` §2 reserves for harness output.

        It goes through the same :meth:`_retain_locked` every other topic does,
        which is the whole reason this is a method here rather than a second
        broker: a graph topic outside the LRU bound would reintroduce exactly
        the unbounded growth C6 removed, one entry per session forever.

        Caller contract, from `docs/architecture.md` §4: the transition is
        already in SQLite. This method broadcasts; it never decides and never
        persists.
        """
        payload: dict[str, object] = {
            "session_id": session_id,
            "node_id": node_id,
            "status": status,
            "ts": ts,
        }
        async with self._lock:
            self._fan_out_locked(f"graph:{session_id}", payload, kind="node_status")

    async def publish_metrics(self, snapshot: SystemSnapshot) -> None:
        """Publish one ephemeral current-state snapshot on ``metrics``."""
        async with self._lock:
            self._sequences["metrics"] += 1
            frame = _EventFrame(
                topic="metrics",
                seq=self._sequences["metrics"],
                payload=snapshot.to_payload(),
                kind="metrics",
            )
            self._latest_metrics = frame
            message = self._wire_event(frame)
            for connection in tuple(self._connections):
                if "metrics" in connection.topics:
                    self._put_locked(connection, message)

    def _fan_out_locked(
        self, topic: str, payload: dict[str, object], *, kind: str = "event"
    ) -> None:
        self._sequences[topic] += 1
        frame = _EventFrame(
            topic=topic,
            seq=self._sequences[topic],
            payload=payload,
            kind=kind,
        )
        self._retain_locked(frame)
        message = self._wire_event(frame)
        for connection in tuple(self._connections):
            if topic in connection.topics:
                self._put_locked(connection, message)

    def _retain_locked(self, frame: _EventFrame) -> None:
        """Append to the topic's window, evicting whole topics past the cap.

        Every drop — a frame ageing out of a full deque, or an entire topic
        being evicted — is recorded in ``_dropped_through`` first, so a later
        cursor lands on :class:`ReplayGapError` and the client refetches from
        REST rather than silently missing events.
        """
        history = self._history.get(frame.topic)
        if history is None:
            history = deque(maxlen=self._history_size)
            self._history[frame.topic] = history
            self._evict_locked()
        else:
            self._history.move_to_end(frame.topic)

        if len(history) == self._history_size:
            self._dropped_through[frame.topic] = history[0].seq
        history.append(frame)

    def _evict_locked(self) -> None:
        while len(self._history) > self._max_topics:
            topic, dropped = self._history.popitem(last=False)
            if dropped:
                self._dropped_through[topic] = dropped[-1].seq
            # A run whose window is gone cannot contribute to its session topic
            # any more either; the pair is retired together so the run -> session
            # map does not outlive what it is for.
            prefix, _, identifier = topic.partition(":")
            if prefix == "run":
                self._run_sessions.pop(identifier, None)

    def _put_locked(self, connection: BrokerConnection, message: WireMessage) -> bool:
        if connection.closed:
            return False
        try:
            connection.queue.put_nowait(message)
        except asyncio.QueueFull:
            self._disconnect_locked(connection)
            return False
        return True

    def _disconnect_locked(self, connection: BrokerConnection) -> None:
        if connection.closed:
            return
        connection.closed = True
        connection.topics.clear()
        self._connections.discard(connection)
        while not connection.queue.empty():
            connection.queue.get_nowait()
        connection.queue.put_nowait(_CLOSED)

    def _wire_event(self, frame: _EventFrame) -> WireMessage:
        return {
            "type": frame.kind,
            "stream": self.stream_id,
            "topic": frame.topic,
            "seq": frame.seq,
            "payload": frame.payload,
        }

    @staticmethod
    def _validate_topic(topic: str) -> None:
        # The public topic vocabulary. Adding an entry is a **wire-format
        # change**: `frontend/src/ws/protocol.ts` holds the matching union and
        # B8 made it the single place the format is written down, so a prefix
        # accepted here and unknown there is a frame the client drops as
        # malformed. `graph:` was added by C9 alongside its frontend note.
        if topic == "metrics":
            return
        prefix, separator, identifier = topic.partition(":")
        if separator and prefix in {"session", "run", "graph"} and identifier:
            return
        raise InvalidTopicError(f"invalid topic {topic!r}")


__all__ = [
    "BrokerConnection",
    "EventBroker",
    "InvalidTopicError",
    "ReplayGapError",
    "WireMessage",
]
