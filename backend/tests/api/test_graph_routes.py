"""C9 graph-resource REST contract: creating a proposal over HTTP."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.harnesses import ADAPTERS, create_adapter
from app.harnesses.base import ParseStats, StructuredRequest, StructuredResult
from app.main import create_app
from app.models.pricing import TokenCounts
from app.orchestrator.graph import DagError, DagErrorKind
from app.orchestrator.planner import (
    PlanFailure,
    PlanFailureKind,
    PlannerChoice,
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
    choice: PlannerChoice | None = None
    final_branch: str | None = None

    async def plan_graph(
        self,
        objective: str,
        *,
        repo_path: Path,
        creator: Any,
        context: str | None = None,
        choice: PlannerChoice | None = None,
        auto_merge: bool = False,
        base_ref: str = "HEAD",
        final_branch: str | None = None,
    ) -> PlanProposal | PlanFailure:
        await creator.validate_repo(
            repo_path,
            base_ref=base_ref,
            final_branch=final_branch,
        )
        self.objective = objective
        self.context = context
        self.choice = choice
        self.final_branch = final_branch
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
            final_branch=final_branch,
        )
        return PlanProposal(
            title="Planner result",
            nodes=nodes,
            usage=planner_usage(),
            attempts=1,
            graph=graph,
        )


class StructuredFake:
    """A planner-capable adapter with no CLI behind it.

    Registered by the ``planner_harness`` fixture so that a test driving the
    *real* planner over HTTP cannot reach a binary. Without it, a regression
    that stopped refusing a bad choice would not fail the test below — it would
    launch a live agent and hang the suite, which is the one failure mode a
    test must never have.
    """

    name = "fake-structured"

    def __init__(self) -> None:
        self.supported_models = ["fake-mini"]
        self.stats = ParseStats()

    async def complete_structured(self, request: StructuredRequest) -> StructuredResult:
        return StructuredResult(
            data={
                "title": "A plan",
                "nodes": [
                    {
                        "id": "only",
                        "title": "Do the thing",
                        "description": "The brief.",
                        "depends_on": [],
                        "acceptance_criteria": ["it compiles"],
                        "suggested_harness": "fake",
                        "suggested_model": MODEL,
                        "estimated_effort": "small",
                        "touches": ["backend/**"],
                    }
                ],
            },
            usage=None,
            model="fake-mini",
        )


@pytest.fixture
def planner_harness(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings whose planner runs :class:`StructuredFake` instead of a CLI."""
    monkeypatch.setitem(ADAPTERS, StructuredFake.name, cast(Any, StructuredFake))
    return settings.model_copy(update={"planner_harness": StructuredFake.name})


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


def test_graph_node_review_policy_round_trips_and_can_be_edited(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "nodes": [
                    node_body("a", requires_review=False),
                    node_body("default-policy"),
                ],
            },
        )

        assert created.status_code == 201, created.text
        payload = created.json()
        policy_by_name = {
            node["name"]: node["requires_review"] for node in payload["nodes"]
        }
        assert policy_by_name == {"a": False, "default-policy": True}

        session_id = payload["session"]["id"]
        node_id = payload["ids_by_name"]["a"]
        replacement = node_body("a", requires_review=True)
        updated = client.put(
            f"/api/sessions/{session_id}/nodes/{node_id}", json=replacement
        )

        assert updated.status_code == 200, updated.text
        assert updated.json()["requires_review"] is True
        fetched = client.get(f"/api/sessions/{session_id}/nodes/{node_id}")
        assert fetched.status_code == 200
        assert fetched.json()["requires_review"] is True


def test_plan_endpoint_persists_and_forwards_the_requested_final_branch(
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
                "objective": "Build it",
                "final_branch": "deliver/planned-result",
            },
        )

        assert response.status_code == 201, response.text
        session = response.json()["session"]
        assert session["final_branch"] == "deliver/planned-result"
        assert session["integration_branch"] != session["final_branch"]
        assert fake.final_branch == "deliver/planned-result"


@pytest.mark.parametrize("branch", ["bad..name", "main"])
def test_plan_rejects_invalid_or_existing_final_branch_before_the_model(
    settings: Settings, target_repo: Path, branch: str
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        fake = FakePlanner()
        client.app.state.planner = fake

        response = client.post(
            "/api/graphs/plan",
            json={
                "repo_path": str(target_repo),
                "objective": "Build it",
                "final_branch": branch,
            },
        )

        assert response.status_code == 422
        assert "branch" in response.json()["detail"]
        assert fake.objective is None


def test_active_sessions_reserve_hierarchically_conflicting_final_branches(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        first = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "final_branch": "release",
                "nodes": [node_body("first")],
            },
        )
        assert first.status_code == 201, first.text

        collision = client.post(
            "/api/graphs",
            json={
                "repo_path": str(target_repo),
                "final_branch": "release/v1",
                "nodes": [node_body("second")],
            },
        )

        assert collision.status_code == 422
        assert "reserved by non-final session" in collision.json()["detail"]
        assert len(client.get("/api/sessions").json()) == 1


def test_plan_endpoint_rejects_a_non_repository_before_calling_the_planner(
    settings: Settings, tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        fake = FakePlanner()
        client.app.state.planner = fake

        response = client.post(
            "/api/graphs/plan",
            json={"repo_path": str(plain), "objective": "Build it"},
        )

    assert response.status_code == 422
    assert "is not a git repository" in response.json()["detail"]
    assert fake.objective is None


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
        (PlanFailureKind.TIMED_OUT, 504),
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


def test_planner_options_are_rendered_from_capabilities_not_a_list(
    settings: Settings,
) -> None:
    """`design.md` §8: everything the UI needs to offer the choice honestly.

    A 200 here also proves the route is declared before ``/{session_id}``;
    registered the other way round, ``planner-options`` would be read as a
    session id and answer 404.
    """
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/graphs/planner-options")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["default"] == {
        "backend": "harness",
        "harness": "claude-code",
        # `null` is a real value: "whatever the CLI is configured for".
        "model": None,
        "selectable": True,
    }

    by_key = {
        (option["backend"], option["harness"]): option for option in payload["options"]
    }
    # Both installed adapters take a schema, so both are offered — and the
    # `api` option is listed unconditionally, because its credential cannot be
    # probed without guessing.
    assert set(by_key) == {
        ("api", None),
        ("harness", "claude-code"),
        ("harness", "codex"),
    }

    api = by_key[("api", None)]
    assert api["is_spend"] is True
    assert api["supports_effort"] is True
    assert api["models"] == settings.planner_api_models

    for (backend, harness), option in by_key.items():
        if backend != "harness":
            continue
        assert harness is not None
        # Invariant 7: a harness plan rides the subscription. And no effort
        # control, because the CLI decides its own depth.
        assert option["is_spend"] is False
        assert option["supports_effort"] is False
        assert option["models"] == create_adapter(harness).supported_models


def test_a_per_plan_choice_reaches_the_planner_intact(
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
                "planner": {"backend": "api", "model": "claude-haiku-4-5"},
            },
        )

        assert response.status_code == 201, response.text
        assert fake.choice == PlannerChoice(
            backend="api",
            harness=None,
            model="claude-haiku-4-5",
            effort=settings.planner_effort,
        )

        # Nothing named is nothing chosen: the planner is told `None` and uses
        # the backend the application owns.
        client.post(
            "/api/graphs/plan",
            json={"repo_path": str(target_repo), "objective": "Again"},
        )
        assert fake.choice is None


def test_saved_planner_defaults_are_used_without_a_form_override(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        fake = FakePlanner()
        client.app.state.planner = fake
        saved = client.put(
            "/api/settings/ai",
            json={
                "planner": {
                    "backend": "harness",
                    "harness": "codex",
                    "model": None,
                },
                "search": {
                    "backend": "harness",
                    "harness": "codex",
                    "model": None,
                },
                "planner_effort": "max",
            },
        )
        assert saved.status_code == 200, saved.text

        response = client.post(
            "/api/graphs/plan",
            json={"repo_path": str(target_repo), "objective": "Use my defaults"},
        )

        assert response.status_code == 201, response.text
        assert fake.choice == PlannerChoice(
            backend="harness",
            harness="codex",
            model=None,
            effort="max",
        )


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"harness": "codx"}, "unknown harness 'codx'"),
        ({"harness": "codex", "model": "claude-opus-5"}, "not one of harness 'codex'"),
        ({"backend": "api", "model": "gpt-5.6-sol"}, "not selectable for the `api`"),
        ({"backend": "api", "harness": "codex"}, "runs no harness"),
    ],
)
def test_a_choice_the_request_got_wrong_is_422_and_says_what_is_valid(
    planner_harness: Settings,
    target_repo: Path,
    choice: dict[str, str],
    expected: str,
) -> None:
    """The person who typed it is the audience, so it is their input that is wrong.

    503 would tell them the server is broken and send them to the logs of a
    machine that is working exactly as configured. The **real** planner is used
    here: a fake would prove the route forwards a choice, not that an unusable
    one is refused before a backend exists.
    """
    settings = planner_harness
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        response = client.post(
            "/api/graphs/plan",
            json={
                "repo_path": str(target_repo),
                "objective": "Build it",
                "planner": choice,
            },
        )

        assert response.status_code == 422, response.text
        assert expected in response.json()["detail"]
        # Refused before anything was planned or written.
        assert client.get("/api/sessions").json() == []


def test_an_unknown_backend_name_never_reaches_the_orchestrator(
    settings: Settings, target_repo: Path
) -> None:
    """A closed set on the wire, so the schema answers before the planner does."""
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        response = client.post(
            "/api/graphs/plan",
            json={
                "repo_path": str(target_repo),
                "objective": "Build it",
                "planner": {"backend": "anthropic"},
            },
        )
        assert response.status_code == 422, response.text


def test_a_harness_the_settings_named_is_still_503_and_not_422(
    settings: Settings, target_repo: Path
) -> None:
    """The other half of the split: a misconfigured server is the operator's.

    Nothing about the request is wrong, so answering 422 would blame the person
    who typed a perfectly good objective. It stays `not_configured` — the 503
    that names the fix — exactly as it was before the choice existed.
    """
    broken = settings.model_copy(update={"planner_harness": "no-such-harness"})
    with TestClient(create_app(broken)) as client:
        install_fake_service(client, broken)
        response = client.post(
            "/api/graphs/plan",
            json={"repo_path": str(target_repo), "objective": "Build it"},
        )

        assert response.status_code == 503, response.text
        assert response.json()["detail"]["kind"] == "not_configured"
        assert client.get("/api/sessions").json() == []


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

        integration_path = Path(str(final["session"]["workspace_root"])) / "integration"  # type: ignore[index]
        for _ in range(200):
            if not integration_path.exists():
                break
            time.sleep(0.01)
        assert not integration_path.exists()

        aggregate = client.get(f"{graph_url}/diff")
        assert aggregate.status_code == 200
        assert "api.txt" in aggregate.json()["patch"]

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
