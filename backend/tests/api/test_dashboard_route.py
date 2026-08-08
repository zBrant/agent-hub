"""Dashboard HTTP contract."""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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


def test_unknown_dashboard_period_is_rejected(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/dashboard?period=year")
    assert response.status_code == 422
