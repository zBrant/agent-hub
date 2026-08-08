"""C9 graph-resource REST contract: creating a proposal over HTTP."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.api.conftest import MODEL, install_fake_service


def node_body(name: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": name,
        "prompt": f"do {name}",
        "harness": "fake",
        "model": MODEL,
    }
    body.update(overrides)
    return body


def test_a_proposal_is_persisted_pending_and_addressable_by_node(
    settings: Settings, target_repo: Path
) -> None:
    """Invariant 6: persisting a proposal starts nothing."""
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "title": "diamond",
                "nodes": [
                    node_body("schema", acceptance_criteria=["tables exist"]),
                    node_body("api", depends_on=["schema"], touches=["backend/**"]),
                    node_body("ui", depends_on=["schema"], estimated_effort="small"),
                    node_body("docs", depends_on=["api", "ui"]),
                ],
            },
        )
        assert created.status_code == 201, created.text
        payload = created.json()
        session_id = payload["session"]["id"]

        assert payload["session"]["status"] == "planning"
        assert payload["session"]["auto_merge"] is False
        assert sorted(payload["ids_by_name"]) == ["api", "docs", "schema", "ui"]
        assert {node["status"] for node in payload["nodes"]} == {"pending"}
        # No worktree was materialized: a node's base is the merge of its
        # parents, and no parent has run.
        assert {node["worktree_path"] for node in payload["nodes"]} == {None}
        by_name = {node["name"]: node for node in payload["nodes"]}
        assert by_name["api"]["touches"] == ["backend/**"]
        assert by_name["ui"]["estimated_effort"] == "small"
        assert by_name["schema"]["acceptance_criteria"] == ["tables exist"]

        # Every node is addressable, which is the surface C3 reported missing.
        listed = client.get(f"/api/sessions/{session_id}/nodes").json()
        assert len(listed) == 4
        for node in listed:
            assert (
                client.get(f"/api/sessions/{session_id}/nodes/{node['id']}").status_code
                == 200
            )

        # And the session-addressed Phase 1 conveniences refuse rather than
        # guess which of the four was meant. That refusal is the orchestrator's.
        assert client.get(f"/api/sessions/{session_id}/node").status_code == 409
        assert client.get(f"/api/sessions/{session_id}/diff").status_code == 409


def test_a_cycle_is_422_with_the_defects_the_planner_needs(
    settings: Settings, target_repo: Path
) -> None:
    """`design.md` §8's correction loop needs node ids, not prose."""
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        response = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "nodes": [
                    node_body("a", depends_on=["b"]),
                    node_body("b", depends_on=["a"]),
                ],
            },
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert [error["kind"] for error in detail["errors"]] == ["cycle"]
        assert len(detail["errors"][0]["nodes"]) == 2


def test_an_unresolvable_depends_on_names_the_planner_slug(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        response = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "nodes": [node_body("a", depends_on=["nonexistent"])],
            },
        )
        assert response.status_code == 422
        errors = response.json()["detail"]["errors"]
        assert [error["kind"] for error in errors] == ["unknown_dependency"]
        assert "nonexistent" in errors[0]["nodes"]


def test_an_invalid_body_never_reaches_the_orchestrator(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        assert (
            client.post(
                "/api/graphs", json={"repo_path": str(target_repo), "nodes": []}
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/graphs",
                json={
                    "repo_path": str(target_repo),
                    "nodes": [node_body("a", prompt="")],
                },
            ).status_code
            == 422
        )
        # An unsupported model is the orchestrator's ValueError, translated to
        # the same 422 rather than a 500.
        assert (
            client.post(
                "/api/graphs",
                json={
                    "repo_path": str(target_repo),
                    "nodes": [node_body("a", model="not-a-model")],
                },
            ).status_code
            == 422
        )
        # Nothing above reached persistence.
        assert client.get("/api/sessions").json() == []

    # The real harness registry, because the fake factory answers to any name.
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/api/graphs",
                json={
                    "repo_path": str(target_repo),
                    "nodes": [node_body("a", harness="missing-harness", model=None)],
                },
            ).status_code
            == 422
        )
        assert client.get("/api/sessions").json() == []


def test_a_graph_node_cannot_be_run_before_the_scheduler_materializes_it(
    settings: Settings, target_repo: Path
) -> None:
    """The gap C9 could not close, asserted so it is not mistaken for working.

    ``POST /nodes/{id}/runs`` calls ``run_node``, which requires a ``ready``
    node with a worktree. A proposal's nodes are ``pending`` and unmaterialized
    — building their base is ``start_node(parents=...)``, and the parents come
    from edges no orchestrator use case exposes to a transport. Driving a graph
    over HTTP therefore needs the scheduler wired to a route; see C9's report.
    """
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "nodes": [node_body("a"), node_body("b", depends_on=["a"])],
            },
        ).json()
        session_id = created["session"]["id"]
        node_id = created["ids_by_name"]["a"]
        refused = client.post(f"/api/sessions/{session_id}/nodes/{node_id}/runs")
        assert refused.status_code == 409
        assert "pending" in refused.json()["detail"]
