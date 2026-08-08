"""C9's graph topic: node transitions over B6's broker, not a second one."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.ws.broker import (
    BrokerConnection,
    EventBroker,
    InvalidTopicError,
    ReplayGapError,
)
from tests.api.conftest import MODEL, git, install_fake_service

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    path = tmp_path / "target"
    path.mkdir()
    asyncio.run(git(path, "init", "-q", "-b", "main"))
    (path / "README.md").write_text("original\n", encoding="utf-8")
    asyncio.run(git(path, "add", "-A"))
    asyncio.run(git(path, "commit", "-qm", "initial"))
    return path


async def transition(broker: EventBroker, node_id: str, status: str, ts: int) -> None:
    await broker.publish_node_status(
        session_id="sess_one", node_id=node_id, status=status, ts=ts
    )


async def nothing_more(connection: BrokerConnection) -> None:
    """Assert the queue is idle. A duplicate replay shows up here."""
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(connection.receive(), timeout=0.01)


# ---------------------------------------------------------------------------
# Broker mechanics
# ---------------------------------------------------------------------------


async def test_a_node_transition_is_delivered_on_the_graph_topic() -> None:
    broker = EventBroker()
    async with broker.connection() as connection:
        await broker.subscribe(connection, "graph:sess_one")
        ready = await connection.receive()
        assert ready is not None and ready["type"] == "ready"

        await transition(broker, "node_a", "running", 1_700)
        frame = await connection.receive()

    assert frame == {
        # A distinct wire type: node status is orchestration state, and the
        # `event` payload is an AgentEvent (architecture §2).
        "type": "node_status",
        "stream": broker.stream_id,
        "topic": "graph:sess_one",
        "seq": 1,
        "payload": {
            "session_id": "sess_one",
            "node_id": "node_a",
            "status": "running",
            "ts": 1_700,
        },
    }


async def test_the_graph_topic_does_not_leak_into_run_or_session_topics() -> None:
    broker = EventBroker()
    async with broker.connection() as connection:
        await broker.subscribe(connection, "session:sess_one")
        assert await connection.receive() is not None  # ready
        await transition(broker, "node_a", "done", 1)
        await nothing_more(connection)


async def test_a_reconnect_replays_transitions_with_no_gap_and_no_duplicate() -> None:
    broker = EventBroker()
    topic = "graph:sess_one"

    async with broker.connection() as first:
        await broker.subscribe(first, topic)
        assert await first.receive() is not None  # ready
        await transition(broker, "node_a", "ready", 1)
        seen = await first.receive()
        assert seen is not None and seen["seq"] == 1

    # The transitions the browser missed are already in SQLite; the bounded
    # window covers the transport gap.
    await transition(broker, "node_a", "running", 2)
    await transition(broker, "node_a", "awaiting_review", 3)

    async with broker.connection() as reconnected:
        await broker.subscribe(
            reconnected, topic, stream=broker.stream_id, after=int(seen["seq"])
        )
        replayed = [await reconnected.receive(), await reconnected.receive()]
        assert [frame["seq"] for frame in replayed if frame is not None] == [2, 3]
        assert [
            frame["payload"]["status"] for frame in replayed if frame is not None
        ] == ["running", "awaiting_review"]
        # No duplicate of seq 1, and nothing invented after seq 3.
        await nothing_more(reconnected)


async def test_a_cursor_older_than_the_window_raises_instead_of_resuming() -> None:
    broker = EventBroker(history_size=2)
    for index in range(5):
        await transition(broker, "node_a", "running", index)

    async with broker.connection() as connection:
        with pytest.raises(ReplayGapError, match="precedes retained history"):
            await broker.subscribe(
                connection, "graph:sess_one", stream=broker.stream_id, after=1
            )
        # The tail is still replayable, so the gap is a real boundary and not a
        # blanket refusal.
        await broker.subscribe(
            connection, "graph:sess_one", stream=broker.stream_id, after=4
        )
        assert await connection.receive() is not None  # ready checkpoint


async def test_an_evicted_graph_topic_reports_a_gap_rather_than_growing() -> None:
    """A new topic type that skipped ``_retain_locked`` would grow forever.

    One entry per session, for the life of the process. Going through the LRU
    means a graph topic can be evicted, and C6's rule then applies to it: every
    drop is recorded, so a stale cursor raises instead of silently resuming at
    the live edge.
    """
    broker = EventBroker(max_topics=2)
    await broker.publish_node_status(
        session_id="sess_a", node_id="node_a", status="done", ts=1
    )
    await broker.publish_node_status(
        session_id="sess_b", node_id="node_b", status="done", ts=2
    )
    await broker.publish_node_status(
        session_id="sess_c", node_id="node_c", status="done", ts=3
    )

    assert len(broker._history) == 2
    async with broker.connection() as connection:
        with pytest.raises(ReplayGapError):
            await broker.subscribe(
                connection, "graph:sess_a", stream=broker.stream_id, after=0
            )


async def test_the_topic_vocabulary_is_still_closed() -> None:
    broker = EventBroker()
    async with broker.connection() as connection:
        for topic in ("graph:", "graphs:sess_one", "graph", "node:node_a"):
            with pytest.raises(InvalidTopicError):
                await broker.subscribe(connection, topic)


# ---------------------------------------------------------------------------
# End to end: an HTTP transition arrives on the socket
# ---------------------------------------------------------------------------


def test_http_transitions_reach_a_subscribed_socket_in_order(
    tmp_path: Path, target_repo: Path
) -> None:
    settings = Settings(
        root=tmp_path / "agenthub", pricing_path=REPO_ROOT / "pricing.yaml"
    )
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/sessions",
            json={
                "repo_path": str(target_repo),
                "prompt": "create api.txt",
                "harness": "fake",
                "model": MODEL,
            },
        ).json()
        session_id = created["session"]["id"]
        node_id = created["node"]["id"]
        base = f"/api/sessions/{session_id}/nodes/{node_id}"

        with client.websocket_connect("/ws") as socket:
            socket.send_json({"type": "subscribe", "topic": f"graph:{session_id}"})
            assert socket.receive_json()["type"] == "ready"

            assert (
                client.post(f"{base}/runs").json()["node_status"] == "awaiting_review"
            )
            first = socket.receive_json()
            assert first["type"] == "node_status"
            assert first["topic"] == f"graph:{session_id}"
            assert first["seq"] == 1
            assert first["payload"]["node_id"] == node_id
            assert first["payload"]["status"] == "awaiting_review"

            assert client.post(f"{base}/approve", json={}).status_code == 200
            second = socket.receive_json()
            assert second["seq"] == 2
            assert second["payload"]["status"] == "done"
