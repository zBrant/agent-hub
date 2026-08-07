"""Ordering, replay, and backpressure contracts for the event broker."""

from __future__ import annotations

import asyncio

import pytest

from app.harnesses.events import AssistantText
from app.ws.broker import EventBroker, ReplayGapError


def event(run_id: str, seq: int) -> AssistantText:
    return AssistantText(run_id=run_id, ts=1_000 + seq, text=f"event-{seq}")


async def test_event_is_published_to_run_and_session_topics() -> None:
    broker = EventBroker()
    await broker.register_run("run_one", "sess_one")

    async with broker.connection() as connection:
        await broker.subscribe(connection, "run:run_one")
        await broker.subscribe(connection, "session:sess_one")
        run_ready = await connection.receive()
        session_ready = await connection.receive()
        assert run_ready is not None and run_ready["type"] == "ready"
        assert session_ready is not None and session_ready["type"] == "ready"

        await broker.publish(event("run_one", 1))
        run_frame = await connection.receive()
        session_frame = await connection.receive()

    assert run_frame is not None and run_frame["topic"] == "run:run_one"
    assert session_frame is not None
    assert session_frame["topic"] == "session:sess_one"
    assert run_frame["payload"] == session_frame["payload"]
    assert run_frame["payload"] == {
        "type": "assistant_text",
        "run_id": "run_one",
        "ts": 1_001,
        "text": "event-1",
    }


async def test_reconnect_replays_without_a_gap_or_duplicate() -> None:
    broker = EventBroker()
    topic = "run:run_one"

    async with broker.connection() as first:
        await broker.subscribe(first, topic)
        ready = await first.receive()
        assert ready is not None
        assert ready["cursor"] == 0
        await broker.publish(event("run_one", 1))
        first_event = await first.receive()
        assert first_event is not None and first_event["seq"] == 1

    # These facts are durable before publish in production. The bounded history
    # covers the transport gap while no browser connection exists.
    await broker.publish(event("run_one", 2))
    await broker.publish(event("run_one", 3))

    async with broker.connection() as reconnected:
        await broker.subscribe(
            reconnected,
            topic,
            stream=str(first_event["stream"]),
            after=int(first_event["seq"]),
        )
        replayed = [await reconnected.receive(), await reconnected.receive()]
        assert [frame["seq"] for frame in replayed if frame is not None] == [2, 3]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(reconnected.receive(), timeout=0.01)


async def test_cursor_older_than_retained_history_is_an_explicit_gap() -> None:
    broker = EventBroker(history_size=2)
    for seq in range(1, 4):
        await broker.publish(event("run_one", seq))

    async with broker.connection() as connection:
        with pytest.raises(ReplayGapError, match="precedes retained history"):
            await broker.subscribe(
                connection,
                "run:run_one",
                stream=broker.stream_id,
                after=0,
            )


async def test_foreign_stream_resets_at_an_atomic_live_checkpoint() -> None:
    broker = EventBroker()
    await broker.publish(event("run_one", 1))

    async with broker.connection() as connection:
        await broker.subscribe(
            connection,
            "run:run_one",
            stream="previous-process",
            after=99,
        )
        ready = await connection.receive()
        assert ready == {
            "type": "ready",
            "stream": broker.stream_id,
            "topic": "run:run_one",
            "cursor": 1,
        }
        await broker.publish(event("run_one", 2))
        live = await connection.receive()
        assert live is not None and live["seq"] == 2


async def test_slow_subscriber_is_disconnected_without_blocking_publish() -> None:
    broker = EventBroker(queue_size=1)
    async with broker.connection() as connection:
        await broker.subscribe(connection, "run:run_one")
        await connection.receive()  # ready
        await broker.publish(event("run_one", 1))
        await broker.publish(event("run_one", 2))

        assert connection.closed is True
        assert broker.connection_count == 0
        assert await connection.receive() is None
