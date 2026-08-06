"""HTTP transport smoke tests."""

from fastapi.testclient import TestClient

from app.main import create_app


def test_health_endpoint_and_lifespan() -> None:
    app = create_app()
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert isinstance(app.state.started_ms, int)


def test_openapi_includes_the_health_contract() -> None:
    schema = create_app().openapi()
    assert schema["info"] == {"title": "AgentHub", "version": "0.1.0"}
    assert schema["paths"]["/health"]["get"]["tags"] == ["system"]
