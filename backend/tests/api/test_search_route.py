"""E1 HTTP smoke test over a real integration worktree and ripgrep."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.api.conftest import MODEL


def test_search_routes_are_typed_and_session_scoped(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        created = client.post(
            "/api/sessions",
            json={
                "repo_path": str(target_repo),
                "prompt": "Create a search target",
                "harness": "codex",
                "model": MODEL,
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["session"]["id"]

        found = client.get(
            "/api/search/text",
            params={"session_id": session_id, "pattern": "original"},
        )
        assert found.status_code == 200
        assert found.json() == {
            "matches": [
                {
                    "path": "README.md",
                    "line": 1,
                    "column": 1,
                    "preview": "original",
                }
            ],
            "truncated": False,
        }

        read = client.get(
            "/api/search/file",
            params={"session_id": session_id, "path": "README.md"},
        )
        assert read.status_code == 200
        assert read.json()["lines"] == [{"line": 1, "text": "original"}]

        listing = client.get(
            "/api/search/directory", params={"session_id": session_id, "path": "."}
        )
        assert listing.status_code == 200
        assert {entry["path"] for entry in listing.json()["entries"]} >= {
            ".git",
            "README.md",
        }

        invalid = client.get(
            "/api/search/text",
            params={"session_id": session_id, "pattern": "["},
        )
        assert invalid.status_code == 400
        assert (
            client.get(
                "/api/search/text",
                params={"session_id": "sess_missing", "pattern": "value"},
            ).status_code
            == 404
        )
