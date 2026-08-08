"""C9 node-addressed REST contract, and the human gate over HTTP."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.orchestrator.service import REVIEW_FEEDBACK_HEADER
from tests.api.conftest import MODEL, install_fake_service


def create_session(
    client: TestClient, target_repo: Path, **overrides: object
) -> dict[str, str]:
    body: dict[str, object] = {
        "repo_path": str(target_repo),
        "prompt": "create api.txt",
        "harness": "fake",
        "model": MODEL,
    }
    body.update(overrides)
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 201, response.text
    payload = response.json()
    return {"session": payload["session"]["id"], "node": payload["node"]["id"]}


# ---------------------------------------------------------------------------
# Addressing
# ---------------------------------------------------------------------------


def test_node_routes_address_the_same_node_the_session_routes_resolve(
    settings: Settings, target_repo: Path
) -> None:
    """Phase 1's single-node flow, driven entirely through node addressing."""
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        ids = create_session(client, target_repo, auto_merge=True)
        session_id, node_id = ids["session"], ids["node"]

        listed = client.get(f"/api/sessions/{session_id}/nodes")
        assert listed.status_code == 200
        assert [node["id"] for node in listed.json()] == [node_id]

        node = client.get(f"/api/sessions/{session_id}/nodes/{node_id}")
        assert node.status_code == 200
        assert node.json()["status"] == "ready"
        # C1's graph columns reach the canvas without a second query.
        assert node.json()["touches"] == []
        assert node.json()["estimated_effort"] is None

        started = client.post(f"/api/sessions/{session_id}/nodes/{node_id}/runs")
        assert started.status_code == 200
        outcome = started.json()
        assert outcome["node_id"] == node_id
        assert outcome["run_status"] == "success"
        assert outcome["node_status"] == "done"
        assert outcome["merged"] is True
        assert outcome["tokens"]["total_tokens"] == 10

        # The session-addressed Phase 1 route agrees, because it resolves to the
        # same node rather than to a different code path.
        assert client.get(f"/api/sessions/{session_id}/node").json()["id"] == node_id
        assert client.get(f"/api/sessions/{session_id}").json()["status"] == "done"


def test_the_phase_1_session_flow_still_works_end_to_end(
    settings: Settings, target_repo: Path
) -> None:
    """The session-addressed routes were kept; this is why they can be.

    ``docs/acceptance-phase-1.md`` records an accepted run against these URLs
    and the committed frontend still calls them. Adding node addressing must not
    change what they do on a one-node session.
    """
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        ids = create_session(client, target_repo, auto_merge=True)
        session_id = ids["session"]

        result = client.post(f"/api/sessions/{session_id}/runs").json()
        assert result["node_status"] == "done"
        assert len(client.get(f"/api/sessions/{session_id}/runs").json()) == 1
        summary = client.get(
            f"/api/sessions/{session_id}/runs/{result['run_id']}/summary"
        ).json()
        assert summary["tokens"]["total_tokens"] == 10
        events = client.get(
            f"/api/sessions/{session_id}/runs/{result['run_id']}/events"
        ).json()
        assert [event["type"] for event in events] == [
            "run_started",
            "usage",
            "run_finished",
        ]
        assert (
            "api.txt" in client.get(f"/api/sessions/{session_id}/diff").json()["patch"]
        )


def test_missing_session_and_missing_node_are_both_404(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        ids = create_session(client, target_repo)
        session_id, node_id = ids["session"], ids["node"]

        assert client.get("/api/sessions/sess_missing/nodes").status_code == 404
        assert client.get("/api/sessions/sess_missing/nodes/node_x").status_code == 404
        assert client.get(f"/api/sessions/{session_id}/nodes/node_x").status_code == 404
        for operation in ("runs", "kill", "retry", "approve"):
            assert (
                client.post(
                    f"/api/sessions/{session_id}/nodes/node_x/{operation}", json={}
                ).status_code
                == 404
            )
        assert (
            client.get(
                f"/api/sessions/{session_id}/nodes/node_x/acceptance"
            ).status_code
            == 404
        )
        assert (
            client.get(f"/api/sessions/{session_id}/nodes/node_x/reviews").status_code
            == 404
        )
        # The node exists but belongs to another session: still 404, never a
        # silent operation on someone else's worktree.
        other = create_session(client, target_repo)
        assert (
            client.get(f"/api/sessions/{other['session']}/nodes/{node_id}").status_code
            == 404
        )


def test_invalid_state_transitions_are_409_not_500(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        ids = create_session(client, target_repo, auto_merge=True)
        session_id, node_id = ids["session"], ids["node"]
        base = f"/api/sessions/{session_id}/nodes/{node_id}"

        # Nothing is running and nothing has run.
        assert client.post(f"{base}/kill").status_code == 409
        assert client.post(f"{base}/retry", json={}).status_code == 409
        assert client.post(f"{base}/approve", json={}).status_code == 409
        assert client.post(f"{base}/reject", json={"feedback": "no"}).status_code == 409

        assert client.post(f"{base}/runs").json()["node_status"] == "done"
        # Done is terminal: a second run is a conflict, not a crash.
        assert client.post(f"{base}/runs").status_code == 409


# ---------------------------------------------------------------------------
# The human gate (C7) over HTTP
# ---------------------------------------------------------------------------


def test_the_checklist_is_readable_and_approval_resolves_it(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        ids = create_session(
            client,
            target_repo,
            acceptance_criteria=["api.txt exists", "nothing else changed"],
        )
        session_id, node_id = ids["session"], ids["node"]
        base = f"/api/sessions/{session_id}/nodes/{node_id}"

        assert client.post(f"{base}/runs").json()["node_status"] == "awaiting_review"

        checklist = client.get(f"{base}/acceptance")
        assert checklist.status_code == 200
        assert [row["criterion"] for row in checklist.json()] == [
            "api.txt exists",
            "nothing else changed",
        ]
        assert {row["outcome"] for row in checklist.json()} == {"unevaluated"}
        assert client.get(f"{base}/acceptance", params={"attempt": 2}).json() == []
        assert client.get(f"{base}/reviews").json() == []

        approved = client.post(
            f"{base}/approve", json={"outcomes": {"0": "pass", "1": "fail"}}
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "merged"

        resolved = client.get(f"{base}/acceptance", params={"attempt": 1}).json()
        assert [row["outcome"] for row in resolved] == ["pass", "fail"]
        reviews = client.get(f"{base}/reviews").json()
        assert [(row["attempt"], row["decision"]) for row in reviews] == [
            (1, "approved")
        ]
        assert client.get(f"{base}").json()["status"] == "done"


def test_rejection_carries_feedback_into_the_next_attempt(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        adapter = install_fake_service(client, settings)
        ids = create_session(client, target_repo, acceptance_criteria=["it works"])
        session_id, node_id = ids["session"], ids["node"]
        base = f"/api/sessions/{session_id}/nodes/{node_id}"

        assert client.post(f"{base}/runs").json()["node_status"] == "awaiting_review"

        rejected = client.post(
            f"{base}/reject",
            json={
                "feedback": "the file is missing a trailing newline",
                "outcomes": {"0": "fail"},
            },
        )
        assert rejected.status_code == 200
        assert rejected.json()["node_status"] == "awaiting_review"

        # B7: a retry is a new Run, and the reviewer's words reach its prompt.
        assert adapter.attempts == 2
        assert REVIEW_FEEDBACK_HEADER in adapter.specs[-1].prompt
        assert "trailing newline" in adapter.specs[-1].prompt
        assert "trailing newline" not in adapter.specs[0].prompt

        reviews = client.get(f"{base}/reviews").json()
        assert [(row["attempt"], row["decision"]) for row in reviews] == [
            (1, "rejected")
        ]
        assert reviews[0]["feedback"] == "the file is missing a trailing newline"
        first_attempt = client.get(f"{base}/acceptance", params={"attempt": 1}).json()
        assert [row["outcome"] for row in first_attempt] == ["fail"]
        second_attempt = client.get(f"{base}/acceptance", params={"attempt": 2}).json()
        assert [row["outcome"] for row in second_attempt] == ["unevaluated"]


def test_a_rejection_without_a_reason_is_refused_by_validation(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        ids = create_session(client, target_repo)
        base = f"/api/sessions/{ids['session']}/nodes/{ids['node']}"
        assert client.post(f"{base}/runs").json()["node_status"] == "awaiting_review"

        # Absent, empty, and whitespace-only: the first two are the schema's, the
        # third is the orchestrator's, and all three are 422.
        assert client.post(f"{base}/reject", json={}).status_code == 422
        assert client.post(f"{base}/reject", json={"feedback": ""}).status_code == 422
        assert (
            client.post(f"{base}/reject", json={"feedback": "   "}).status_code == 422
        )
