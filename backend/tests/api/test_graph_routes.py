"""C9 graph-resource REST contract: creating a proposal over HTTP."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.pricing import TokenCounts
from app.orchestrator.graph import DagError, DagErrorKind
from app.orchestrator.planner import (
    PlanFailure,
    PlanFailureKind,
    PlannerUsage,
    PlanProposal,
)
from app.orchestrator.service import PlannedNode
from tests.api.conftest import MODEL, install_fake_service


def planner_usage() -> PlannerUsage:
    return PlannerUsage(
        model="claude-sonnet-4-5",
        counts=TokenCounts(
            input_tokens=11,
            output_tokens=7,
            cache_read_tokens=5,
            cache_write_tokens=3,
        ),
        cost_usd=0.00123,
        price_table_version=1,
        requests=1,
    )


@dataclass
class FakePlanner:
    failure: PlanFailure | None = None
    objective: str | None = None
    context: str | None = None

    async def plan_graph(
        self,
        objective: str,
        *,
        repo_path: Path,
        creator: Any,
        context: str | None = None,
        auto_merge: bool = False,
        base_ref: str = "HEAD",
    ) -> PlanProposal | PlanFailure:
        self.objective = objective
        self.context = context
        if self.failure is not None:
            return self.failure
        nodes = (
            PlannedNode(
                name="backend",
                prompt="implement the endpoint",
                harness="fake",
                model=MODEL,
                acceptance_criteria=("endpoint is tested",),
                touches=("backend/**",),
            ),
            PlannedNode(
                name="frontend",
                prompt="build the client",
                harness="fake",
                model=MODEL,
                depends_on=("backend",),
            ),
        )
        graph = await creator.create_graph(
            repo_path=repo_path,
            nodes=nodes,
            title="Planner result",
            auto_merge=auto_merge,
            base_ref=base_ref,
        )
        return PlanProposal(
            title="Planner result",
            nodes=nodes,
            usage=planner_usage(),
            attempts=1,
            graph=graph,
        )


def node_body(name: str, **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "name": name,
        "prompt": f"do {name}",
        "harness": "fake",
        "model": MODEL,
    }
    body.update(overrides)
    return body


def test_plan_endpoint_persists_a_gated_proposal_from_an_objective(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        fake = FakePlanner()
        client.app.state.planner = fake

        response = client.post(
            "/api/graphs/plan",
            json={
                "repo_path": str(target_repo),
                "objective": "Build an endpoint and its client",
                "context": "Keep the transport thin",
            },
        )

        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["status"] == "proposal"
        assert payload["session"]["status"] == "planning"
        assert payload["session"]["auto_merge"] is False
        assert {node["status"] for node in payload["nodes"]} == {"pending"}
        assert payload["ids_by_name"].keys() == {"backend", "frontend"}
        assert payload["attempts"] == 1
        assert payload["planner_usage"] == {
            "model": "claude-sonnet-4-5",
            "requests": 1,
            "tokens": {
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 5,
                "cache_write_tokens": 3,
                "total_tokens": 26,
            },
            "cost_usd": 0.00123,
            "price_table_version": 1,
            # Whether `cost_usd` is money or invariant 7's estimated
            # equivalent. Asserted here because the route is where a client
            # learns it, and a client that renders the cost without it will
            # mislabel one of the two backends.
            "is_spend": True,
        }
        assert fake.objective == "Build an endpoint and its client"
        assert fake.context == "Keep the transport thin"


def test_plan_failure_is_typed_and_persists_nothing(
    settings: Settings, target_repo: Path
) -> None:
    failure = PlanFailure(
        kind=PlanFailureKind.INVALID_GRAPH,
        message="the planner did not produce a valid graph",
        usage=planner_usage(),
        attempts=3,
        errors=(
            DagError(
                kind=DagErrorKind.CYCLE,
                nodes=("backend", "frontend"),
                message="dependency cycle: backend -> frontend -> backend",
            ),
        ),
    )
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        client.app.state.planner = FakePlanner(failure=failure)

        response = client.post(
            "/api/graphs/plan",
            json={"repo_path": str(target_repo), "objective": "Build it"},
        )

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["kind"] == "invalid_graph"
        assert detail["attempts"] == 3
        assert detail["errors"] == [
            {
                "kind": "cycle",
                "nodes": ["backend", "frontend"],
                "message": "dependency cycle: backend -> frontend -> backend",
            }
        ]
        assert client.get("/api/sessions").json() == []


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (PlanFailureKind.NOT_CONFIGURED, 503),
        (PlanFailureKind.API_ERROR, 502),
        (PlanFailureKind.REFUSED, 422),
        (PlanFailureKind.TRUNCATED, 422),
        (PlanFailureKind.MALFORMED, 422),
    ],
)
def test_every_plan_failure_has_a_status_that_points_somewhere(
    settings: Settings,
    target_repo: Path,
    kind: PlanFailureKind,
    expected: int,
) -> None:
    """The status code has to say who can fix it.

    503 for a missing credential rather than 502: nothing upstream was reached,
    and the operator would otherwise go looking for an Anthropic outage instead
    of at their own environment. Parametrized over the whole enum so a new
    failure kind cannot quietly inherit 422.
    """
    failure = PlanFailure(kind=kind, message="no", usage=planner_usage(), attempts=1)
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        client.app.state.planner = FakePlanner(failure=failure)

        response = client.post(
            "/api/graphs/plan",
            json={"repo_path": str(target_repo), "objective": "Build it"},
        )

        assert response.status_code == expected
        assert response.json()["detail"]["kind"] == kind.value


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
        graph_refused = client.post(f"/api/graphs/{session_id}/runs")
        assert graph_refused.status_code == 409
        assert "approve" in graph_refused.json()["detail"]


def test_edit_approve_run_and_node_reads_close_the_graph_contract(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "auto_merge": True,
                "nodes": [node_body("a"), node_body("b"), node_body("remove-me")],
            },
        ).json()
        session_id = created["session"]["id"]
        a = created["ids_by_name"]["a"]
        b = created["ids_by_name"]["b"]
        remove_me = created["ids_by_name"]["remove-me"]
        graph_url = f"/api/graphs/{session_id}"
        nodes_url = f"/api/sessions/{session_id}/nodes"

        graph = client.get(graph_url)
        assert graph.status_code == 200
        assert graph.json()["edges"] == []

        removed = client.delete(f"{nodes_url}/{remove_me}")
        assert removed.status_code == 200
        assert {node["id"] for node in removed.json()["nodes"]} == {a, b}

        replacement = {
            "name": "renamed-b",
            "prompt": "implement b",
            "acceptance_criteria": ["b works"],
            "harness": "fake",
            "model": MODEL,
            "touches": ["backend/**"],
            "estimated_effort": "small",
        }
        updated = client.put(f"{nodes_url}/{b}", json=replacement)
        assert updated.status_code == 200
        assert updated.json()["name"] == "renamed-b"

        edged = client.put(f"{nodes_url}/{b}/dependencies/{a}")
        assert edged.status_code == 200
        assert [
            (edge["node_id"], edge["depends_on_id"]) for edge in edged.json()["edges"]
        ] == [(b, a)]
        cycle = client.put(f"{nodes_url}/{a}/dependencies/{b}")
        assert cycle.status_code == 422
        assert cycle.json()["detail"]["errors"][0]["kind"] == "cycle"
        assert len(client.get(graph_url).json()["edges"]) == 1

        approved = client.post(f"{graph_url}/approve")
        assert approved.status_code == 200
        assert {node["id"]: node["status"] for node in approved.json()["nodes"]} == {
            a: "ready",
            b: "pending",
        }
        assert client.put(f"{nodes_url}/{b}", json=replacement).status_code == 409
        assert client.delete(f"{nodes_url}/{b}/dependencies/{a}").status_code == 409

        started = client.post(f"{graph_url}/runs")
        assert started.status_code == 202
        assert started.json() == {"session_id": session_id, "scheduled": True}

        final: dict[str, object] = {}
        for _ in range(200):
            final = client.get(graph_url).json()
            if {node["status"] for node in final["nodes"]} == {"done"}:  # type: ignore[index]
                break
            time.sleep(0.01)
        assert {node["status"] for node in final["nodes"]} == {"done"}  # type: ignore[index]

        for node_id in (a, b):
            base = f"{nodes_url}/{node_id}"
            runs = client.get(f"{base}/runs")
            assert runs.status_code == 200
            assert len(runs.json()) == 1
            run_id = runs.json()[0]["id"]
            assert (
                client.get(f"{base}/runs/{run_id}/summary").json()["tokens"][
                    "total_tokens"
                ]
                == 10
            )
            assert [
                event["type"]
                for event in client.get(f"{base}/runs/{run_id}/events").json()
            ] == ["run_started", "usage", "run_finished"]
            assert "api.txt" in client.get(f"{base}/diff").json()["patch"]

        resolved = client.patch(
            f"{nodes_url}/{b}/acceptance/1", json={"outcomes": {"0": "pass"}}
        )
        assert resolved.status_code == 200
        assert [row["outcome"] for row in resolved.json()] == ["pass"]
