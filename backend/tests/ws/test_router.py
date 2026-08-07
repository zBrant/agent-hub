"""WebSocket transport integration against the real application lifespan."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.harnesses.events import AssistantText
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_one_socket_subscribes_and_receives_a_canonical_event(tmp_path: Path) -> None:
    settings = Settings(
        root=tmp_path / "agenthub-root",
        pricing_path=REPO_ROOT / "pricing.yaml",
    )
    app = create_app(settings)
    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        websocket.send_json({"type": "subscribe", "topic": "run:run_one"})
        ready = websocket.receive_json()
        assert ready["type"] == "ready"
        assert ready["cursor"] == 0

        event = AssistantText(run_id="run_one", ts=1_000, text="hello")
        assert client.portal is not None
        client.portal.call(app.state.broker.publish, event)
        frame = websocket.receive_json()

        assert frame["type"] == "event"
        assert frame["topic"] == "run:run_one"
        assert frame["seq"] == 1
        assert frame["payload"] == event.model_dump(mode="json")


def test_invalid_control_frame_reports_an_error_without_closing(tmp_path: Path) -> None:
    settings = Settings(
        root=tmp_path / "agenthub-root",
        pricing_path=REPO_ROOT / "pricing.yaml",
    )
    with TestClient(create_app(settings)) as client:
        with client.websocket_connect("/ws") as websocket:
            websocket.send_json({"type": "subscribe", "topic": "not-a-topic"})
            assert websocket.receive_json()["code"] == "invalid_frame"
            websocket.send_json({"type": "subscribe", "topic": "metrics"})
            assert websocket.receive_json()["type"] == "ready"
