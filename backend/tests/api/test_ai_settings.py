"""REST contract for persistent global AI defaults."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def test_get_ai_settings_uses_deployment_defaults_and_capabilities(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/settings/ai")

    assert response.status_code == 200
    body = response.json()
    assert body["planner"] == {
        "backend": settings.planner_backend,
        "harness": settings.planner_harness,
        "model": settings.planner_harness_model,
    }
    assert body["search"] == {
        "backend": settings.search_backend,
        "harness": settings.search_harness,
        "model": settings.search_harness_model,
    }
    assert body["planner_effort"] == settings.planner_effort

    planner_api = next(
        option for option in body["planner_options"] if option["backend"] == "api"
    )
    search_api = next(
        option for option in body["search_options"] if option["backend"] == "api"
    )
    assert planner_api == {
        "backend": "api",
        "harness": None,
        "models": settings.planner_api_models,
        "is_spend": True,
        "supports_effort": True,
    }
    assert search_api == {
        "backend": "api",
        "harness": None,
        "models": settings.search_api_models,
        "is_spend": True,
        "supports_effort": False,
    }
    assert all(
        option["harness"] is not None
        and option["is_spend"] is False
        and option["supports_effort"] is False
        for option in body["planner_options"]
        if option["backend"] == "harness"
    )


def test_put_ai_settings_persists_across_process_lifetimes(
    settings: Settings,
) -> None:
    payload = {
        "planner": {"backend": "harness", "harness": "codex", "model": None},
        "search": {
            "backend": "api",
            "harness": None,
            "model": settings.search_api_models[0],
        },
        "planner_effort": "max",
    }

    with TestClient(create_app(settings)) as client:
        response = client.put("/api/settings/ai", json=payload)
        assert response.status_code == 200
        assert response.json()["planner"] == payload["planner"]
        assert response.json()["search"] == payload["search"]
        assert response.json()["planner_effort"] == "max"

    # A new application and service read the authored row from SQLite rather
    # than falling back to the same in-memory Settings object.
    with TestClient(create_app(settings)) as client:
        persisted = client.get("/api/settings/ai")
    assert persisted.status_code == 200
    assert persisted.json()["planner"] == payload["planner"]
    assert persisted.json()["search"] == payload["search"]
    assert persisted.json()["planner_effort"] == "max"


@pytest.mark.parametrize(
    ("selection", "detail"),
    [
        (
            {"backend": "api", "harness": "codex", "model": "claude-opus-5"},
            "API backend runs without a harness",
        ),
        (
            {"backend": "api", "harness": None, "model": "not-a-model"},
            "API model must be one of",
        ),
        (
            {"backend": "harness", "harness": "not-a-harness", "model": None},
            "must support structured completion",
        ),
    ],
)
def test_put_ai_settings_rejects_invalid_selections_without_persisting(
    settings: Settings,
    selection: dict[str, str | None],
    detail: str,
) -> None:
    payload = {
        "planner": selection,
        "search": {"backend": "harness", "harness": "codex", "model": None},
        "planner_effort": "high",
    }
    with TestClient(create_app(settings)) as client:
        response = client.put("/api/settings/ai", json=payload)
        current = client.get("/api/settings/ai")

    assert response.status_code == 422
    assert detail in response.json()["detail"]
    assert current.json()["planner"]["backend"] == settings.planner_backend


def test_ai_settings_openapi_contract_has_no_secret_fields(tmp_path: Path) -> None:
    settings = Settings(root=tmp_path / "agenthub")
    schema = create_app(settings).openapi()
    operation = schema["paths"]["/api/settings/ai"]

    assert set(operation) == {"get", "put"}
    request_schema = schema["components"]["schemas"]["AiSettingsRequest"]
    assert set(request_schema["properties"]) == {
        "planner",
        "search",
        "planner_effort",
    }
    selection_schema = schema["components"]["schemas"]["AiRuntimeSelectionRequest"]
    assert set(selection_schema["properties"]) == {"backend", "harness", "model"}
