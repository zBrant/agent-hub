"""Dashboard HTTP contract."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.metrics.system import AgentProcessMetric, SystemSnapshot


def test_empty_dashboard_is_typed_and_zero_not_invented_cost(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/dashboard?period=7d")

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"] == "7d"
    assert payload["usage"]["tokens"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }
    assert payload["usage"]["estimated_equivalent_cost_usd"] is None
    assert payload["active_sessions"] == []
    assert payload["node_completion_rate"] is None
    assert payload["event_feed"] == []


def test_unknown_dashboard_period_is_rejected(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/dashboard?period=year")
    assert response.status_code == 422


def test_current_system_snapshot_uses_the_generated_contract(
    settings: Settings,
) -> None:
    app = create_app(settings)
    snapshot = SystemSnapshot(
        ts=20,
        cpu_percent=34.5,
        cpu_per_core=(20.0, 49.0),
        memory_total_bytes=1_000,
        memory_used_bytes=600,
        memory_available_bytes=400,
        memory_percent=60,
        swap_total_bytes=0,
        swap_used_bytes=0,
        swap_free_bytes=0,
        swap_percent=0,
        disk_total_bytes=2_000,
        disk_used_bytes=500,
        disk_free_bytes=1_500,
        disk_percent=25,
        processes=(
            AgentProcessMetric(
                node_id="node_live",
                pid=123,
                harness="codex",
                rss_bytes=256,
                cpu_percent=12.5,
                uptime_ms=5_000,
                process_count=2,
            ),
        ),
    )
    with TestClient(app) as client:
        # Lifespan cleanup owns its original sampler local, so replacing only
        # the route-facing state makes this transport test deterministic.
        app.state.system_sampler = SimpleNamespace(latest=snapshot)
        response = client.get("/api/dashboard/system")

    assert response.status_code == 200
    payload = response.json()
    assert payload["cpu_percent"] == 34.5
    assert payload["cpu_per_core"] == [20.0, 49.0]
    assert payload["processes"] == [
        {
            "node_id": "node_live",
            "pid": 123,
            "harness": "codex",
            "rss_bytes": 256,
            "cpu_percent": 12.5,
            "uptime_ms": 5_000,
            "process_count": 2,
        }
    ]
