"""The planner: objective in, validated proposal out, no live API call.

Every test here drives the **real** Anthropic SDK — the real schema transform,
the real request body, the real response parsing — over an `httpx.MockTransport`
serving recorded bodies. A fake client object would have proved that our code
calls a method we wrote the signature of; this proves the SDK accepts the schema
we generate and that `parse()` hands back what we think it does. Nothing here
opens a socket, and no test costs money.

The one exception is `test_live_plan`, which is marked `harness` and skipped
unless `AGENTHUB_RUN_LIVE_HARNESS=1`: only a live call can prove the API accepts
this module's JSON Schema, and that is a paid, opt-in check.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, cast
from unittest import mock

import httpx
import pytest
from anthropic import AsyncAnthropic
from structlog.testing import capture_logs

from app.config import Settings
from app.models.pricing import PriceTable, TokenCounts, load_price_table
from app.models.status import NodeStatus, SessionStatus
from app.orchestrator.graph import Dag, DagErrorKind, InvalidDag
from app.orchestrator.planner import (
    ApiPlanBackend,
    HarnessPlanBackend,
    PlanBackendUnavailable,
    PlanFailure,
    PlanFailureKind,
    PlannedActivity,
    Planner,
    PlanProposal,
    PlanResponse,
    PlanTurn,
    UnavailablePlanBackend,
    _flatten_schema,
    _strict_schema,
    compose_prompt,
    correction_prompt,
    create_backend,
    create_planner,
    harness_catalog,
    objective_prompt,
    to_planned_nodes,
    validate_plan,
)
from app.orchestrator.service import NodeRunService, PlannedNode
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "planner"

#: Stands in for a real key everywhere. Distinctive enough that a substring
#: search over a SQLite file or a log stream is a meaningful assertion.
CANARY_KEY = "sk-ant-api03-CANARY-MUST-NEVER-LEAK"

OBJECTIVE = "Add JWT authentication to the backend API and a login page."

CATALOG: dict[str, tuple[str, ...]] = {
    "claude-code": ("claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"),
    "codex": ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


async def git(cwd: Path, *args: str) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(cwd),
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await process.communicate()
    output = stdout.decode()
    assert process.returncode == 0, f"git {args} failed:\n{output}"
    return output


@pytest.fixture
async def target_repo(tmp_path: Path) -> Path:
    path = tmp_path / "target"
    path.mkdir()
    await git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("original\n", encoding="utf-8")
    await git(path, "add", "-A")
    await git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(root=tmp_path / "agenthub", pricing_path=PRICING_YAML)


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    upgrade_database_sync(settings.database_url)
    database = Database.from_settings(settings)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def prices() -> PriceTable:
    return load_price_table(PRICING_YAML)


@pytest.fixture
def service(
    database: Database, settings: Settings, prices: PriceTable
) -> NodeRunService:
    """The real write path. `create_graph` needs no CLI: it builds an adapter
    only to read `supported_models`, and launches nothing."""
    return NodeRunService(database=database, settings=settings, prices=prices)


# ---------------------------------------------------------------------------
# A recorded API
# ---------------------------------------------------------------------------

RECORDED: dict[str, Any] = json.loads(
    (FIXTURES / "valid_plan.json").read_text(encoding="utf-8")
)


def recorded_plan() -> dict[str, Any]:
    """The plan inside the recorded body, as a mutable dict."""
    text = RECORDED["content"][1]["text"]
    assert isinstance(text, str)
    plan: dict[str, Any] = json.loads(text)
    return plan


def body(
    plan: dict[str, Any] | None = None,
    *,
    stop_reason: str = "end_turn",
    text: str | None = None,
) -> dict[str, Any]:
    """The recorded response body, with the plan (or the content) replaced.

    ``plan=None`` and no ``text`` gives empty content, which is the shape a
    refusal arrives in.
    """
    reply = copy.deepcopy(RECORDED)
    reply["stop_reason"] = stop_reason
    if text is not None:
        reply["content"] = [{"type": "text", "text": text}]
    elif plan is None:
        reply["content"] = []
    else:
        reply["content"][1]["text"] = json.dumps(plan)
    return reply


def activity(
    slug: str,
    *,
    depends_on: Sequence[str] = (),
    harness: str = "claude-code",
    model: str = "claude-opus-5",
    effort: str = "medium",
    touches: Sequence[str] = ("backend/**",),
    criteria: Sequence[str] = ("it compiles",),
) -> dict[str, Any]:
    return {
        "id": slug,
        "title": f"Do {slug}",
        "description": f"The brief for {slug}.",
        "depends_on": list(depends_on),
        "acceptance_criteria": list(criteria),
        "suggested_harness": harness,
        "suggested_model": model,
        "estimated_effort": effort,
        "touches": list(touches),
    }


def plan_of(*activities: dict[str, Any], title: str = "A plan") -> dict[str, Any]:
    return {"title": title, "nodes": list(activities)}


@dataclass
class Reply:
    body: dict[str, Any] | None = None
    status: int = 200


@dataclass
class FakeApi:
    """`POST /v1/messages`, replaying `replies` and remembering the requests.

    The last reply repeats forever, so "the planner never corrects itself" is
    one entry rather than three copies.
    """

    replies: list[Reply]
    requests: list[dict[str, Any]] = field(default_factory=list)
    headers: list[dict[str, str]] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        reply = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        if reply.body is None:
            return httpx.Response(
                reply.status,
                json={"type": "error", "error": {"type": "api_error", "message": "x"}},
            )
        return httpx.Response(reply.status, json=reply.body)

    @property
    def messages(self) -> list[list[dict[str, Any]]]:
        """The `messages` array of each request, in order."""
        return [request["messages"] for request in self.requests]


def make_planner(
    api: FakeApi,
    settings: Settings,
    prices: PriceTable,
    *,
    catalog: Mapping[str, Sequence[str]] | None = None,
    api_key: str | None = CANARY_KEY,
) -> Planner:
    client = AsyncAnthropic(
        api_key=api_key,
        base_url="https://api.anthropic.invalid",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        # Otherwise a 500 becomes three requests and every count below drifts.
        max_retries=0,
    )
    return Planner(
        backend=ApiPlanBackend(client=client, settings=settings),
        settings=settings,
        prices=prices,
        catalog=CATALOG if catalog is None else catalog,
    )


# ---------------------------------------------------------------------------
# Pure: schema, translation, validation, prompts
# ---------------------------------------------------------------------------


def test_recorded_plan_matches_the_schema() -> None:
    """The fixture is the wire contract; a field rename must break here first."""
    plan = PlanResponse.model_validate(recorded_plan())
    assert [node.id for node in plan.nodes] == [
        "db_schema",
        "auth_api",
        "auth_ui",
        "auth_tests",
    ]
    assert plan.nodes[3].depends_on == ["auth_api", "auth_ui"]


def test_schema_carries_no_unsupported_constraints() -> None:
    """Structured output supports neither numeric nor length bounds.

    The SDK moves such a constraint into the field description and enforces it
    client-side, which would turn a violation into a `ValidationError` raised
    out of `parse()` instead of a defect the correction loop can hand back. So
    the schema must not contain one.
    """
    banned = {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
    }

    def keys_of(node: object) -> set[str]:
        if isinstance(node, dict):
            found = set(node)
            for value in node.values():
                found |= keys_of(value)
            return found
        if isinstance(node, list):
            return {key for item in node for key in keys_of(item)}
        return set()

    assert keys_of(PlanResponse.model_json_schema()) & banned == set()


def test_compose_prompt_folds_title_and_description() -> None:
    assert compose_prompt("Add auth", "Do the thing.") == "# Add auth\n\nDo the thing."
    assert compose_prompt("", "Only a brief.") == "Only a brief."
    assert compose_prompt("Only a title", "  ") == "# Only a title"


def test_to_planned_nodes_maps_design_md_field_names() -> None:
    nodes = to_planned_nodes(
        PlanResponse.model_validate(recorded_plan()),
        catalog=CATALOG,
        fallback_harness="claude-code",
    )
    by_name = {node.name: node for node in nodes}
    assert set(by_name) == {"db_schema", "auth_api", "auth_ui", "auth_tests"}

    # suggested_harness/suggested_model land on harness/model (design.md §8).
    assert by_name["auth_ui"].harness == "codex"
    assert by_name["auth_ui"].model == "gpt-5.6-terra"
    assert by_name["auth_tests"].depends_on == ("auth_api", "auth_ui")
    assert by_name["db_schema"].estimated_effort == "small"
    assert by_name["db_schema"].touches == (
        "backend/app/models/tables.py",
        "backend/migrations/versions/**",
    )
    assert by_name["auth_api"].acceptance_criteria[0].startswith("POST /api/auth/login")
    assert by_name["auth_api"].prompt.startswith(
        "# Implement the authentication endpoints\n\n"
    )


def test_unknown_harness_falls_back_and_unsupported_model_is_dropped() -> None:
    """A suggestion the operator is about to overwrite is not worth an attempt."""
    response = PlanResponse.model_validate(
        plan_of(
            activity("a", harness="opencode", model="whatever"),
            activity("b", harness="codex", model="claude-opus-5"),
        )
    )
    nodes = to_planned_nodes(response, catalog=CATALOG, fallback_harness="claude-code")
    assert (nodes[0].harness, nodes[0].model) == ("claude-code", None)
    # A real harness with a model belonging to the other one: keep the harness,
    # drop the model to "the harness's own default".
    assert (nodes[1].harness, nodes[1].model) == ("codex", None)


def test_translation_never_repairs_structure() -> None:
    """Ids and edges pass through verbatim so `build_dag` can see the defect."""
    response = PlanResponse.model_validate(
        plan_of(activity(" padded "), activity("b", depends_on=["ghost"]))
    )
    nodes = to_planned_nodes(response, catalog=CATALOG, fallback_harness="claude-code")
    assert nodes[0].name == " padded "
    assert nodes[1].depends_on == ("ghost",)


def test_validate_plan_accepts_the_recorded_diamond() -> None:
    nodes = to_planned_nodes(
        PlanResponse.model_validate(recorded_plan()),
        catalog=CATALOG,
        fallback_harness="claude-code",
    )
    dag = validate_plan(nodes)
    assert isinstance(dag, Dag)
    assert dag.order[0] == "db_schema"
    assert dag.dependencies_of("auth_tests") == ("auth_api", "auth_ui")


def test_validate_plan_reports_orphan_and_cycle_together() -> None:
    nodes = to_planned_nodes(
        PlanResponse.model_validate(
            plan_of(
                activity("a", depends_on=["b"]),
                activity("b", depends_on=["a"]),
                activity("c", depends_on=["nowhere"]),
            )
        ),
        catalog=CATALOG,
        fallback_harness="claude-code",
    )
    dag = validate_plan(nodes)
    assert isinstance(dag, InvalidDag)
    kinds = {error.kind for error in dag.errors}
    assert kinds == {DagErrorKind.CYCLE, DagErrorKind.UNKNOWN_DEPENDENCY}
    assert dag.cycles == (("a", "b"),)


def test_correction_prompt_names_the_cycle_and_every_defect() -> None:
    invalid = validate_plan(
        (
            PlannedNode(name="a", prompt="", harness="codex", depends_on=("b",)),
            PlannedNode(name="b", prompt="", harness="codex", depends_on=("a",)),
            PlannedNode(name="c", prompt="", harness="codex", depends_on=("ghost",)),
        )
    )
    assert isinstance(invalid, InvalidDag)
    message = correction_prompt(invalid)
    assert "cycle: a -> b -> a" in message
    assert "'c' depends on unknown node 'ghost'" in message
    assert "acyclic" in message


def test_objective_prompt_lists_the_catalog_and_the_context() -> None:
    rendered = objective_prompt(
        OBJECTIVE, catalog=CATALOG, context="A FastAPI backend and a Vite frontend."
    )
    assert OBJECTIVE in rendered
    assert "- `codex` — models: gpt-5.6-sol, gpt-5.6-terra, gpt-5.6-luna" in rendered
    assert "A FastAPI backend and a Vite frontend." in rendered


def test_system_prompt_pushes_against_sibling_file_overlap() -> None:
    """`design.md` §12's highest-impact risk; `touches` is the only mitigation."""
    from app.orchestrator.planner import SYSTEM_PROMPT

    assert "touches" in SYSTEM_PROMPT
    assert "disjoint" in SYSTEM_PROMPT
    assert "conflict" in SYSTEM_PROMPT


def test_harness_catalog_comes_from_the_registry() -> None:
    catalog = harness_catalog()
    assert set(catalog) == {"claude-code", "codex"}
    assert "claude-opus-5" in catalog["claude-code"]


def test_planner_refuses_a_fallback_harness_without_an_adapter(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi([Reply(body())])
    settings.planner_fallback_harness = "opencode"
    with pytest.raises(ValueError, match="no adapter"):
        make_planner(api, settings, prices)


# ---------------------------------------------------------------------------
# The request the SDK actually sends
# ---------------------------------------------------------------------------


async def test_request_carries_the_schema_and_the_configured_knobs(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi([Reply(body(recorded_plan()))])
    settings.planner_effort = "xhigh"
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanProposal)
    (request,) = api.requests
    assert request["model"] == settings.planner_model == "claude-opus-5"
    assert request["max_tokens"] == settings.planner_max_tokens
    assert request["output_config"]["effort"] == "xhigh"
    # Thinking is adaptive by default on this model; pinning it would replace
    # the model's judgement with a constant.
    assert "thinking" not in request
    schema = request["output_config"]["format"]["schema"]
    assert request["output_config"]["format"]["type"] == "json_schema"
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["PlannedActivity"]["additionalProperties"] is False
    assert set(schema["$defs"]["PlannedActivity"]["required"]) == set(
        PlannedActivity.model_fields
    )


# ---------------------------------------------------------------------------
# Proposal, correction, failure
# ---------------------------------------------------------------------------


async def test_recorded_response_becomes_a_proposal(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi([Reply(body(recorded_plan()))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanProposal)
    assert result.attempts == 1
    assert result.title == "JWT authentication for the backend API"
    assert [node.name for node in result.nodes] == [
        "db_schema",
        "auth_api",
        "auth_ui",
        "auth_tests",
    ]
    assert result.graph is None  # propose() persists nothing


async def test_a_cycle_costs_exactly_one_correction_round_trip(
    settings: Settings, prices: PriceTable
) -> None:
    cyclic = recorded_plan()
    cyclic["nodes"][0]["depends_on"] = ["auth_tests"]
    api = FakeApi([Reply(body(cyclic)), Reply(body(recorded_plan()))])

    with capture_logs() as logs:
        result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanProposal)
    assert result.attempts == 2
    assert len(api.requests) == 2
    assert result.usage.requests == 2

    # The conversation grows: the rejected plan as the assistant turn, the
    # correction as the next user turn.
    first, second = api.messages
    assert [message["role"] for message in first] == ["user"]
    assert [message["role"] for message in second] == ["user", "assistant", "user"]
    correction = second[2]["content"]
    assert "cycle: auth_api -> auth_tests -> db_schema -> auth_api" in correction
    # The model is shown its own rejected plan, not asked to start over.
    assert "auth_tests" in second[1]["content"]

    events = [entry["event"] for entry in logs]
    assert "planner.invalid_graph" in events
    assert "planner.proposed" in events


async def test_three_bad_attempts_fail_naming_the_cycle(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi(
        [
            Reply(
                body(
                    plan_of(
                        activity("a", depends_on=["b"]), activity("b", depends_on=["a"])
                    )
                )
            )
        ]
    )
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.INVALID_GRAPH
    assert result.attempts == settings.planner_max_attempts == 3
    assert len(api.requests) == 3
    assert "cycle: a -> b -> a" in result.message
    assert {error.kind for error in result.errors} == {DagErrorKind.CYCLE}


async def test_the_attempt_bound_is_configurable(
    settings: Settings, prices: PriceTable
) -> None:
    settings.planner_max_attempts = 1
    api = FakeApi([Reply(body(plan_of(activity("a", depends_on=["a"]))))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert len(api.requests) == 1
    assert "depends on itself" in result.message


async def test_orphan_depends_on_is_caught_before_persistence(
    settings: Settings, prices: PriceTable
) -> None:
    """A slug that names a node the planner never emitted.

    Unreachable once rows exist — C1's foreign keys see to that — so this is
    the only layer that can report it, and it reports it in the planner's own
    vocabulary rather than as a `node_<ULID>`.
    """
    orphaned = recorded_plan()
    orphaned["nodes"][1]["depends_on"] = ["db_schemaa"]
    api = FakeApi([Reply(body(orphaned))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.INVALID_GRAPH
    assert {error.kind for error in result.errors} == {DagErrorKind.UNKNOWN_DEPENDENCY}
    assert "'auth_api' depends on unknown node 'db_schemaa'" in result.message
    # The correction told the model which slug was wrong.
    assert "db_schemaa" in api.messages[1][2]["content"]


async def test_refusal_is_an_outcome_not_an_exception(
    settings: Settings, prices: PriceTable
) -> None:
    """HTTP 200, `stop_reason: refusal`, and content that is not a plan."""
    api = FakeApi([Reply(body(stop_reason="refusal"))])
    with capture_logs() as logs:
        result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.REFUSED
    assert "refusal" in result.message
    assert len(api.requests) == 1  # not retried: a refusal is not a defect
    assert "planner.refused" in [entry["event"] for entry in logs]
    # The tokens the refusal cost are still counted.
    assert result.usage.counts.total > 0


async def test_truncation_is_reported_with_the_knob_to_turn(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi([Reply(body(recorded_plan(), stop_reason="max_tokens"))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.TRUNCATED
    assert "AGENTHUB_PLANNER_MAX_TOKENS" in result.message


async def test_prose_instead_of_json_is_reported(
    settings: Settings, prices: PriceTable
) -> None:
    """`parse()` validates inside the SDK, so this never reaches `stop_reason`."""
    api = FakeApi([Reply(body(text="I would rather write you a poem."))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.MALFORMED


async def test_an_empty_plan_is_reported_rather_than_created(
    settings: Settings, prices: PriceTable
) -> None:
    """`Dag` accepts the empty graph; the product does not."""
    api = FakeApi([Reply(body(plan_of()))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.MALFORMED
    assert "no activities" in result.message


async def test_api_error_is_data(settings: Settings, prices: PriceTable) -> None:
    api = FakeApi([Reply(status=500)])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.API_ERROR
    assert "HTTP 500" in result.message
    assert result.usage.requests == 0


async def test_a_missing_credential_is_data_and_not_a_crash(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, prices: PriceTable
) -> None:
    """No credential is a named failure, not a 500.

    The SDK reports it as a bare `TypeError` from a private `_validate_headers`
    hook, so it slipped past `except APIError` and reached the browser as
    "Internal Server Error" — the whole Sessions tab, whose only button is
    "Create proposal", was unusable with nothing on screen saying why.

    Separate from `API_ERROR`: no request left the machine, so `requests` is 0
    and the correction loop must not spend its budget retrying.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    api = FakeApi([Reply(body(recorded_plan()))])

    result = await make_planner(api, settings, prices, api_key=None).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.NOT_CONFIGURED
    assert "ANTHROPIC_API_KEY" in result.message
    assert result.attempts == 1
    assert result.usage.requests == 0
    assert api.requests == []


async def test_an_unrelated_type_error_still_propagates(
    settings: Settings, prices: PriceTable
) -> None:
    """The credential catch is matched on a message, so it must stay narrow.

    A `TypeError` from anywhere else in the request path is a bug in this
    module, and reporting it as "no credential" would send the operator to look
    for a key that is already there.
    """
    api = FakeApi([Reply(body(recorded_plan()))])
    client = AsyncAnthropic(
        api_key=CANARY_KEY,
        base_url="https://api.anthropic.invalid",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(api.handler)),
        max_retries=0,
    )
    backend = ApiPlanBackend(client=client, settings=settings)

    async def boom(**_kwargs: object) -> object:
        raise TypeError("parse() got an unexpected keyword argument")

    with (
        mock.patch.object(client.messages, "parse", boom),
        pytest.raises(TypeError, match="unexpected keyword"),
    ):
        await backend.request([PlanTurn(role="user", text=OBJECTIVE)])


async def test_an_empty_objective_is_programmer_error(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi([Reply(body(recorded_plan()))])
    with pytest.raises(ValueError, match="objective"):
        await make_planner(api, settings, prices).propose("   ")
    assert api.requests == []


# ---------------------------------------------------------------------------
# Usage: real spend, four fields
# ---------------------------------------------------------------------------


async def test_usage_counts_all_four_fields_and_prices_at_ingest(
    settings: Settings, prices: PriceTable
) -> None:
    api = FakeApi([Reply(body(recorded_plan()))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanProposal)
    expected = TokenCounts(
        input_tokens=1842,
        output_tokens=1507,
        cache_read_tokens=24576,
        cache_write_tokens=3072,
        cache_write_5m_tokens=3072,
        cache_write_1h_tokens=0,
    )
    assert result.usage.counts == expected
    # Invariant 3: `input_tokens` alone is ~6% of the truth on this response.
    assert result.usage.counts.total == 30997
    assert result.usage.cost_usd == prices.cost_usd("claude-opus-5", expected)
    assert result.usage.price_table_version == prices.version
    assert result.usage.model == "claude-opus-5"


async def test_usage_accumulates_across_a_correction(
    settings: Settings, prices: PriceTable
) -> None:
    cyclic = recorded_plan()
    cyclic["nodes"][0]["depends_on"] = ["auth_tests"]
    api = FakeApi([Reply(body(cyclic)), Reply(body(recorded_plan()))])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanProposal)
    assert result.usage.counts.total == 2 * 30997
    assert result.usage.requests == 2


async def test_an_inconsistent_cache_split_does_not_fail_the_plan(
    settings: Settings, prices: PriceTable
) -> None:
    """`TokenCounts` rejects a split that does not sum; usage must not raise."""
    reply = body(recorded_plan())
    reply["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 1,
        "ephemeral_1h_input_tokens": 1,
    }
    api = FakeApi([Reply(reply)])
    result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanProposal)
    assert result.usage.counts.cache_write_tokens == 3072
    assert result.usage.counts.cache_write_5m_tokens == 0


# ---------------------------------------------------------------------------
# Persistence: a proposal, pending and unmaterialized (invariant 6)
# ---------------------------------------------------------------------------


async def test_plan_graph_persists_a_pending_unmaterialized_proposal(
    settings: Settings,
    prices: PriceTable,
    database: Database,
    service: NodeRunService,
    target_repo: Path,
) -> None:
    api = FakeApi([Reply(body(recorded_plan()))])
    result = await make_planner(api, settings, prices).plan_graph(
        OBJECTIVE, repo_path=target_repo, creator=service
    )

    assert isinstance(result, PlanProposal)
    assert result.graph is not None
    ids = result.graph.ids_by_name

    async with database.session() as db_session:
        graph = await Repository(db_session).load_graph(result.graph.session.id)
    assert graph is not None

    assert graph.session.status is SessionStatus.PLANNING
    assert graph.session.auto_merge is False
    assert graph.session.title == "JWT authentication for the backend API"

    for node in graph.nodes:
        assert node.status is NodeStatus.PENDING
        # Unmaterialized: a node's base is the merge of its parents, and no
        # parent has run. Nothing may execute before approval (invariant 6).
        assert node.worktree_path is None
        assert node.branch is None

    # Slugs became node ids, and every edge survived the mapping.
    assert set(ids) == {"db_schema", "auth_api", "auth_ui", "auth_tests"}
    assert all(node_id.startswith("node_") for node_id in ids.values())
    assert graph.depends_on() == {
        ids["db_schema"]: frozenset(),
        ids["auth_api"]: frozenset({ids["db_schema"]}),
        ids["auth_ui"]: frozenset({ids["auth_api"]}),
        ids["auth_tests"]: frozenset({ids["auth_api"], ids["auth_ui"]}),
    }

    by_name = {node.name: node for node in graph.nodes}
    assert by_name["auth_ui"].harness == "codex"
    assert by_name["auth_ui"].model == "gpt-5.6-terra"
    assert by_name["db_schema"].estimated_effort == "small"
    assert len(by_name["auth_api"].acceptance_criteria) == 3
    assert by_name["auth_tests"].touches == (
        "backend/tests/api/test_auth.py",
        "frontend/e2e/login.spec.ts",
    )


async def test_an_incorrigible_planner_writes_no_rows(
    settings: Settings,
    prices: PriceTable,
    database: Database,
    service: NodeRunService,
    target_repo: Path,
) -> None:
    api = FakeApi(
        [
            Reply(
                body(
                    plan_of(
                        activity("a", depends_on=["b"]), activity("b", depends_on=["a"])
                    )
                )
            )
        ]
    )
    result = await make_planner(api, settings, prices).plan_graph(
        OBJECTIVE, repo_path=target_repo, creator=service
    )

    assert isinstance(result, PlanFailure)
    assert "cycle: a -> b -> a" in result.message

    async with database.session() as db_session:
        assert await Repository(db_session).list_sessions() == []
    # Not even a workspace: validation happens before create_graph is called.
    assert not settings.workspaces_root.exists()


async def test_a_refusal_writes_no_rows(
    settings: Settings,
    prices: PriceTable,
    database: Database,
    service: NodeRunService,
    target_repo: Path,
) -> None:
    api = FakeApi([Reply(body(stop_reason="refusal"))])
    result = await make_planner(api, settings, prices).plan_graph(
        OBJECTIVE, repo_path=target_repo, creator=service
    )

    assert isinstance(result, PlanFailure)
    async with database.session() as db_session:
        assert await Repository(db_session).list_sessions() == []


# ---------------------------------------------------------------------------
# The credential (`docs/conventions.md` §6)
# ---------------------------------------------------------------------------


def _files_under(root: Path) -> Iterable[Path]:
    return (path for path in root.rglob("*") if path.is_file())


async def test_the_credential_reaches_the_api_and_nothing_else(
    settings: Settings,
    prices: PriceTable,
    database: Database,
    service: NodeRunService,
    target_repo: Path,
) -> None:
    """The key must be in the request and in nothing that outlives it.

    The first assertion is what makes the rest able to fail: the canary really
    is the credential in play, so finding it on disk or in a log would be a
    real leak rather than a string that was never there.
    """
    api = FakeApi([Reply(body(recorded_plan()))])
    planner = make_planner(api, settings, prices)

    with capture_logs() as logs:
        result = await planner.plan_graph(
            OBJECTIVE, repo_path=target_repo, creator=service
        )
    assert isinstance(result, PlanProposal)

    assert api.headers[0]["x-api-key"] == CANARY_KEY

    rendered_logs = json.dumps(logs, default=str)
    assert CANARY_KEY not in rendered_logs
    assert repr(result) and CANARY_KEY not in repr(result)

    # Everything the planner caused to be written: the SQLite projection, the
    # session workspace, meta and run logs if any existed.
    leaked = [
        path
        for path in _files_under(settings.root)
        if CANARY_KEY.encode() in path.read_bytes()
    ]
    assert leaked == []


async def test_a_failure_message_never_carries_the_credential(
    settings: Settings, prices: PriceTable
) -> None:
    """An error rendered into the UI is the easiest place for a key to escape."""
    api = FakeApi([Reply(status=401)])
    with capture_logs() as logs:
        result = await make_planner(api, settings, prices).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert api.headers[0]["x-api-key"] == CANARY_KEY
    assert CANARY_KEY not in result.message
    assert CANARY_KEY not in json.dumps(logs, default=str)


async def test_the_objective_is_never_logged_verbatim(
    settings: Settings, prices: PriceTable
) -> None:
    """A prompt is untrusted, possibly secret content: hash and length only."""
    secret_objective = f"Rotate the key {CANARY_KEY} across the deployment."
    api = FakeApi([Reply(body(recorded_plan()))])
    with capture_logs() as logs:
        await make_planner(api, settings, prices).propose(secret_objective)

    rendered = json.dumps(logs, default=str)
    assert CANARY_KEY not in rendered
    assert "objective_sha" in rendered


def test_the_api_backend_is_built_without_reading_the_key(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, prices: PriceTable
) -> None:
    """A bare client resolves the key, the auth token, or an `ant auth login`
    profile — reading the variable here would break the third."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    planner = create_planner(
        settings.model_copy(update={"planner_backend": "api"}), prices
    )
    assert set(planner.catalog) == {"claude-code", "codex"}


def test_the_harness_backend_is_the_default_and_needs_no_credential(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, prices: PriceTable
) -> None:
    """`design.md` §8: the default backend has to work on a subscription.

    A Max/Pro plan is not API access, so an `api` default means the Sessions
    tab is dead on a fresh machine until somebody buys credit. This is the test
    that fails if the default is ever flipped back.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert settings.planner_backend == "harness"

    backend = create_backend(settings)

    # Not merely "it built something": `create_backend` degrades to
    # `UnavailablePlanBackend` rather than raising, so asserting on the catalog
    # alone would pass with a planner that answers 503 to everything.
    assert isinstance(backend, HarnessPlanBackend)
    assert set(create_planner(settings, prices).catalog) == {"claude-code", "codex"}


async def test_an_unbuildable_backend_degrades_instead_of_taking_the_server_down(
    settings: Settings, prices: PriceTable
) -> None:
    """The planner is one feature of five, and must not gate the other four.

    Refusing to boot over a misconfigured planner would take away the
    scheduler, the dashboards and code search to punish one broken thing. So
    the failure is deferred to whoever asks for a plan, and arrives as
    `NOT_CONFIGURED` — a 503 naming the fix — rather than as a dead server.
    """
    broken = settings.model_copy(update={"planner_harness": "no-such-harness"})

    backend = create_backend(broken)
    assert isinstance(backend, UnavailablePlanBackend)

    result = await Planner(
        backend=backend, settings=broken, prices=prices, catalog=CATALOG
    ).propose(OBJECTIVE)

    assert isinstance(result, PlanFailure)
    assert result.kind is PlanFailureKind.NOT_CONFIGURED
    assert "no-such-harness" in result.message
    assert result.usage.requests == 0


def test_a_harness_without_structured_output_is_refused_at_construction(
    settings: Settings,
) -> None:
    """A misconfiguration fails where it is configured, not on first use.

    Deferring it to the first objective would report "the planner is not
    configured" to whoever typed the goal, hours after the person who chose the
    harness had gone.
    """

    class Mute:
        name = "mute"
        supported_models: ClassVar[list[str]] = []

    with pytest.raises(PlanBackendUnavailable, match="cannot return schema-validated"):
        HarnessPlanBackend(adapter=cast(Any, Mute()), settings=settings)


# ---------------------------------------------------------------------------
# The one paid check
# ---------------------------------------------------------------------------


@pytest.mark.harness
@pytest.mark.skipif(
    os.environ.get("AGENTHUB_RUN_LIVE_HARNESS") != "1",
    reason="live paid turn; set AGENTHUB_RUN_LIVE_HARNESS=1",
)
async def test_live_plan(tmp_path: Path, prices: PriceTable) -> None:
    """Only a live call can prove the API accepts this module's JSON Schema.

    Deliberately cheap: low effort, a small ceiling and a tiny objective. It is
    the schema being accepted that matters, not the quality of the plan.
    """
    live = Settings(
        root=tmp_path / "agenthub",
        pricing_path=PRICING_YAML,
        planner_effort="low",
        planner_max_tokens=4096,
        planner_max_attempts=1,
    )
    planner = create_planner(live, prices)
    result = await planner.propose(
        "Add a /healthz endpoint to a FastAPI app and a test for it."
    )
    assert isinstance(result, PlanProposal), getattr(result, "message", result)
    assert result.nodes
    assert isinstance(validate_plan(result.nodes), Dag)


def test_the_schema_handed_to_an_adapter_is_strict_and_resolved() -> None:
    """`StructuredRequest`'s contract, checked at the only place that builds one.

    Codex rejects a schema whose objects omit `additionalProperties: false`
    (`invalid_json_schema`, exit 1) and Claude Code accepts it, so a planner
    tested only against Claude would ship broken on Codex. Resolution matters
    for the same reason: Pydantic emits `$defs`/`$ref` and the CLIs disagree
    about them.
    """
    schema = _strict_schema(_flatten_schema(PlanResponse.model_json_schema()))
    rendered = json.dumps(schema)

    assert "$ref" not in rendered
    assert "$defs" not in rendered

    objects = 0

    def check(node: object) -> None:
        nonlocal objects
        if isinstance(node, list):
            for item in node:
                check(item)
            return
        if not isinstance(node, dict):
            return
        if node.get("type") == "object":
            objects += 1
            assert node.get("additionalProperties") is False, node.get("title")
            assert set(node.get("required", [])) >= set(node.get("properties", {}))
        for value in node.values():
            check(value)

    check(schema)
    # The plan is a root object plus the activity items; if this stops being
    # true the walk above may be checking nothing.
    assert objects == 2


def test_an_optional_field_is_refused_rather_than_silently_made_required() -> None:
    """Filling in `required` would change the question, so it is only checked.

    Promoting an optional field to mandatory to satisfy Codex would quietly
    ask the model for something the schema's author said was optional. Raising
    here surfaces it at construction, where `create_backend` turns it into a
    logged startup error and a 503, instead of as a Codex 400 on somebody's
    first objective.
    """
    loose = {
        "type": "object",
        "properties": {"title": {"type": "string"}, "note": {"type": "string"}},
        "required": ["title"],
    }
    with pytest.raises(ValueError, match=r"not strict.*'note'"):
        _strict_schema(loose)
