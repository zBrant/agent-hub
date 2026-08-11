"""HTTP smoke tests for project-and-branch code search."""

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.search.tools import CodeSearchService
from tests.api.conftest import MODEL, git


def _register_project(client: TestClient, target_repo: Path, title: str) -> None:
    response = client.post(
        "/api/sessions",
        json={
            "repo_path": str(target_repo),
            "prompt": "Register this repository with AgentHub",
            "title": title,
            "harness": "codex",
            "model": MODEL,
        },
    )
    assert response.status_code == 201, response.text


def test_search_routes_target_a_deduplicated_project_and_local_branch(
    settings: Settings, target_repo: Path
) -> None:
    asyncio.run(git(target_repo, "checkout", "-qb", "feature/search"))
    (target_repo / "README.md").write_text("feature only\n", encoding="utf-8")
    asyncio.run(git(target_repo, "add", "README.md"))
    asyncio.run(git(target_repo, "commit", "-qm", "feature content"))
    asyncio.run(git(target_repo, "checkout", "-q", "main"))

    app = create_app(settings)
    with TestClient(app) as client:
        # Sessions only teach AgentHub that the repository exists. They are not
        # exposed as Search targets, and duplicates collapse to one project.
        _register_project(client, target_repo, "First activity")
        _register_project(client, target_repo, "Second activity")

        catalog = client.get("/api/search/projects")
        assert catalog.status_code == 200, catalog.text
        projects = catalog.json()["projects"]
        assert len(projects) == 1
        project = projects[0]
        assert project["name"] == "target"
        assert project["repo_path"] == str(target_repo)
        assert {branch["name"] for branch in project["branches"]} == {
            "feature/search",
            "main",
        }
        assert (
            next(branch for branch in project["branches"] if branch["name"] == "main")[
                "is_head"
            ]
            is True
        )
        project_id = project["id"]

        found = client.get(
            "/api/search/text",
            params={
                "project_id": project_id,
                "branch": "main",
                "pattern": "original",
            },
        )
        assert found.status_code == 200, found.text
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

        feature = client.get(
            "/api/search/text",
            params={
                "project_id": project_id,
                "branch": "feature/search",
                "pattern": "feature only",
            },
        )
        assert feature.status_code == 200, feature.text
        assert feature.json()["matches"][0]["preview"] == "feature only"

        script = target_repo.parent / "fake-sg"
        script.write_text(
            """#!/usr/bin/env python3
import json

print(json.dumps({
    "text": "original",
    "range": {
        "start": {"line": 0, "column": 0},
        "end": {"line": 0, "column": 8},
    },
    "file": "README.md",
    "lines": "original",
    "language": "Python",
}))
""",
            encoding="utf-8",
        )
        script.chmod(0o755)
        app.state.search = CodeSearchService(
            app.state.database,
            sg_binary=str(script),
        )
        structural = client.get(
            "/api/search/structural",
            params={
                "project_id": project_id,
                "branch": "main",
                "pattern": "$VALUE",
                "language": "python",
            },
        )
        assert structural.status_code == 200, structural.text
        assert structural.json() == found.json()

        read = client.get(
            "/api/search/file",
            params={
                "project_id": project_id,
                "branch": "main",
                "path": "README.md",
            },
        )
        assert read.status_code == 200
        assert read.json()["lines"] == [{"line": 1, "text": "original"}]
        assert len(read.json()["content_hash"]) == 64

        listing = client.get(
            "/api/search/directory",
            params={"project_id": project_id, "branch": "main", "path": "."},
        )
        assert listing.status_code == 200
        assert listing.json()["entries"] == [{"path": "README.md", "kind": "file"}]

        invalid_pattern = client.get(
            "/api/search/text",
            params={
                "project_id": project_id,
                "branch": "main",
                "pattern": "[",
            },
        )
        assert invalid_pattern.status_code == 400
        assert (
            client.get(
                "/api/search/text",
                params={
                    "project_id": project_id,
                    "branch": "missing",
                    "pattern": "value",
                },
            ).status_code
            == 404
        )
        unanswered = client.post(
            "/api/search/answer",
            json={
                "project_id": "proj_missing",
                "branch": "main",
                "question": "Where is the business rule enforced?",
            },
        )
        assert unanswered.status_code == 404
        blank = client.post(
            "/api/search/answer",
            json={"project_id": project_id, "branch": "main", "question": "   "},
        )
        assert blank.status_code == 422
        assert client.get("/api/search/branches").status_code == 404
