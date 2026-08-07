"""Bounded in-memory fan-out for durable ``AgentEvent`` facts.

The broker is deliberately not persistence. NDJSON and SQLite are written
before :meth:`publish` is called; this module only bridges that durable state to
live clients. A slow client is disconnected instead of ever delaying ingest.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import uuid4

from app.harnesses.events import AgentEvent, agent_event_adapter
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

    def __init__(self, *, queue_size: int = 256, history_size: int = 256) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        if history_size < 1:
            raise ValueError("history_size must be positive")
        self.stream_id = uuid4().hex
        self._queue_size = queue_size
        self._history_size = history_size
        self._connections: set[BrokerConnection] = set()
        self._history: dict[str, deque[_EventFrame]] = defaultdict(
            lambda: deque(maxlen=self._history_size)
        )
        self._sequences: dict[str, int] = defaultdict(int)
        self._run_sessions: dict[RunId, SessionId] = {}
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

            history = self._history[topic]
            if history and after < history[0].seq - 1:
                raise ReplayGapError(
                    f"cursor {after} precedes retained history at {history[0].seq}"
                )
            connection.topics.add(topic)
            for frame in history:
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
                self._sequences[topic] += 1
                frame = _EventFrame(
                    topic=topic,
                    seq=self._sequences[topic],
                    payload=payload,
                )
                self._history[topic].append(frame)
                message = self._wire_event(frame)
                for connection in tuple(self._connections):
                    if topic in connection.topics:
                        self._put_locked(connection, message)

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
            "type": "event",
            "stream": self.stream_id,
            "topic": frame.topic,
            "seq": frame.seq,
            "payload": frame.payload,
        }

    @staticmethod
    def _validate_topic(topic: str) -> None:
        if topic == "metrics":
            return
        prefix, separator, identifier = topic.partition(":")
        if separator and prefix in {"session", "run"} and identifier:
            return
        raise InvalidTopicError(f"invalid topic {topic!r}")


__all__ = [
    "BrokerConnection",
    "EventBroker",
    "InvalidTopicError",
    "ReplayGapError",
    "WireMessage",
]
