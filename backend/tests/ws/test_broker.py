"""Ordering, replay, and backpressure contracts for the event broker."""

from __future__ import annotations

import asyncio

import pytest

from app.harnesses.events import AgentEvent, AssistantText, Usage
from app.metrics.system import SystemSnapshot
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


def system_snapshot(ts: int) -> SystemSnapshot:
    return SystemSnapshot(
        ts=ts,
        cpu_percent=float(ts),
        cpu_per_core=(float(ts),),
        memory_total_bytes=100,
        memory_used_bytes=50,
        memory_available_bytes=50,
        memory_percent=50,
        swap_total_bytes=0,
        swap_used_bytes=0,
        swap_free_bytes=0,
        swap_percent=0,
        disk_total_bytes=100,
        disk_used_bytes=25,
        disk_free_bytes=75,
        disk_percent=25,
        processes=(),
    )


async def test_metrics_subscription_gets_only_current_snapshot_then_live() -> None:
    broker = EventBroker(history_size=2)
    for stamp in range(1, 5):
        await broker.publish_metrics(system_snapshot(stamp))

    async with broker.connection() as connection:
        await broker.subscribe(
            connection,
            "metrics",
            stream=broker.stream_id,
            after=1,
        )
        current = await connection.receive()
        assert current is not None
        assert current["type"] == "metrics"
        assert current["seq"] == 4
        assert current["payload"]["ts"] == 4
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connection.receive(), timeout=0.01)

        await broker.publish_metrics(system_snapshot(5))
        live = await connection.receive()
        assert live is not None and live["seq"] == 5

    # Metrics have their own one-snapshot retention and do not consume the
    # durable topic LRU or its history window.
    assert "metrics" not in broker._history


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


# ---------------------------------------------------------------------------
# Bounded retention
# ---------------------------------------------------------------------------


def usage_event(run_id: str) -> AgentEvent:
    return Usage(run_id=run_id, ts=1, model="gpt-5.6-terra", input_tokens=1)


async def test_topics_are_bounded_so_a_long_lived_server_does_not_grow() -> None:
    """A graph opens one ``run:<id>`` topic per node and never closes it.

    Without a cap the retained frames grow with every node the orchestrator has
    ever run — 256 of them per topic, for the life of the process.
    """
    broker = EventBroker(max_topics=4)

    for index in range(10):
        run_id = f"run_{index}"
        await broker.register_run(run_id, "sess_1")
        await broker.publish(usage_event(run_id))

    # Four run topics plus the session topic they all fan out to; the session
    # topic keeps being republished, so it is never the least recent.
    assert len(broker._history) <= 5
    assert "session:sess_1" in broker._history


async def test_an_evicted_topic_reports_a_gap_not_a_clean_resume() -> None:
    """The failure mode a bound must not introduce.

    A client reconnecting with a cursor into a topic whose history was dropped
    must be told, not silently told it is up to date — otherwise it resumes at
    the live edge and never learns it skipped everything in between.
    """
    broker = EventBroker(max_topics=2)
    await broker.register_run("run_a", "sess_a")
    await broker.publish(usage_event("run_a"))

    # A second node on the same session pushes "run:run_a" out of the window.
    await broker.register_run("run_b", "sess_a")
    await broker.publish(usage_event("run_b"))

    async with broker.connection() as connection:
        with pytest.raises(ReplayGapError):
            await broker.subscribe(
                connection, "run:run_a", stream=broker.stream_id, after=0
            )


async def test_a_frame_ageing_out_of_a_full_window_is_also_a_gap() -> None:
    """The pre-existing bound, asserted through the same mechanism."""
    broker = EventBroker(history_size=2)
    await broker.register_run("run_a", "sess_a")
    for _ in range(5):
        await broker.publish(usage_event("run_a"))

    async with broker.connection() as connection:
        with pytest.raises(ReplayGapError):
            await broker.subscribe(
                connection, "run:run_a", stream=broker.stream_id, after=1
            )
        # The tail is still replayable.
        await broker.subscribe(
            connection, "run:run_a", stream=broker.stream_id, after=4
        )


async def test_retiring_a_run_topic_drops_its_session_mapping() -> None:
    """``_run_sessions`` is one entry per run and outlived everything else."""
    # Two slots: one run topic and the session topic it fans out to, which is
    # exactly what a single node occupies. A cap of one would evict the run
    # topic by its own session topic on the very first publish.
    broker = EventBroker(max_topics=2)
    await broker.register_run("run_a", "sess_a")
    await broker.publish(usage_event("run_a"))
    assert "run_a" in broker._run_sessions

    await broker.register_run("run_b", "sess_a")
    await broker.publish(usage_event("run_b"))

    assert "run_a" not in broker._run_sessions
    assert "run_b" in broker._run_sessions
