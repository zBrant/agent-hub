"""HTTP transport smoke tests."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_health_endpoint_and_lifespan(tmp_path: Path) -> None:
    settings = Settings(
        root=tmp_path / "agenthub-root",
        pricing_path=REPO_ROOT / "pricing.yaml",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
        assert isinstance(app.state.started_ms, int)
        assert app.state.database.url == settings.database_url
        assert app.state.orchestrator is not None
    assert settings.db_path.exists()


def test_openapi_includes_the_health_contract() -> None:
    schema = create_app().openapi()
    assert schema["info"] == {"title": "AgentHub", "version": "0.1.0"}
    assert schema["paths"]["/health"]["get"]["tags"] == ["system"]
