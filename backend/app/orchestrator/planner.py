"""Objective in, validated graph proposal out (`design.md` §8).

**The planner calls the Anthropic API directly; it is not a harness.** That is
`design.md` §8's decision, not this module's: the CLI's
``--output-format stream-json`` structures the *event envelope* and not the
assistant's content, there is no CLI equivalent of ``output_config.format``, and
a harness-routed planner would therefore be prompting for JSON and parsing
prose — exactly what "structured output, never markdown parsing" rules out.

Three consequences shape everything below.

**A valid schema is not a valid DAG.** Structured output guarantees the fields
exist; it cannot express "no cycles". So the response is translated into
:class:`~app.orchestrator.service.PlannedNode` values and run through the pure
core's :func:`~app.orchestrator.graph.build_dag` **before** a row is written,
and a defect is handed back to the model to correct rather than raised at the
UI. The loop is bounded at :attr:`~app.config.Settings.planner_max_attempts`:
an LLM that cannot close a two-node cycle in three tries will not close it in
thirty, and a planner that spins is worse than one that reports.

**Validation happens on the planner's own slugs.** `design.md` §8: the ``id``
the model invents is a local slug and ``depends_on`` refers to slugs, so an
orphan ``depends_on`` only exists before persistence — C1's foreign keys make it
unreachable once rows exist. Reporting the defect in the planner's vocabulary is
also what makes the correction message actionable: ``node 'auth_api' depends on
unknown node 'db_schema'`` names something the model can edit, where a
``node_01J…`` does not. The slug-to-id allocation itself belongs to
:meth:`~app.orchestrator.service.NodeRunService.create_graph`, which is the one
path into the database and does it entirely in memory before its first INSERT.

**Failure is a value, not an exception** (`docs/architecture.md` §9). A refusal,
an unreachable API, a response that will not validate and an incorrigible cycle
are all ordinary outcomes of asking a model for a graph; every one of them
returns a :class:`PlanFailure` carrying enough detail for C9 to render it.
:class:`ValueError` is still raised for programmer error.

Everything that can be pure is pure and lives at the top of this file
(`docs/architecture.md` §3): the response schema, the translation into
``PlannedNode``, the DAG check and the correction prompt are all plain functions
over plain values, testable with no client, no key and no network.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types import MessageParam, ParsedMessage
from anthropic.types import Usage as AnthropicUsage
from pydantic import BaseModel, Field, ValidationError

from app.config import PlannerEffort, Settings
from app.harnesses import ADAPTERS, create_adapter
from app.models.pricing import PriceTable, TokenCounts
from app.orchestrator.graph import Dag, DagError, GraphNode, InvalidDag, build_dag
from app.orchestrator.service import CreatedGraph, PlannedNode

log = structlog.get_logger()


# ---------------------------------------------------------------------------
# The prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are the planner of AgentHub, a local orchestrator that executes a dependency
graph of software activities. Each activity you emit is handed verbatim to an
autonomous coding agent that works alone, in its own git worktree, branched from
the merge of that activity's dependencies, and whose result is merged back into
one shared integration branch.

Decompose the operator's objective into that graph.

## What an activity is

A coherent unit of work one agent can finish in one session, ending in a commit
that leaves the repository working. Aim for the smallest number of activities
that still lets independent work run in parallel — typically 3 to 10. A node per
file is noise; "implement the feature" as a single node is not a graph.

Do not invent activities the repository does not need: no "set up the project",
no "install dependencies", no "review the code", no "merge the branches".
AgentHub merges, and a human reviews. Never write an activity whose work is done
by a person.

## Dependencies

`depends_on` lists the `id` of other activities **in this same response**. It
means "this activity cannot start until that one's changes are in its base". The
graph must be acyclic, no activity may depend on itself, and every id you
reference must be one you emitted.

Declare a dependency only for a real ordering constraint. Every unnecessary edge
serializes work that could have run at the same time.

## Minimizing conflicts — the constraint that matters most

Two activities with no dependency path between them run **concurrently, in
separate worktrees, and are merged into one branch**. If they edit the same
file, that merge conflicts, the node is blocked, and a human has to resolve it
by hand. Parallelism you cannot merge is worse than no parallelism.

So: partition the work by file ownership. Sibling activities — anything that can
run at the same time — must touch disjoint sets of files. When two pieces of
work genuinely need the same file, do not run them in parallel: make one depend
on the other, or merge them into a single activity.

`touches` is where you state that partition: the glob patterns the activity will
modify. Be specific (`backend/app/auth/**`, `frontend/src/routes/Login.tsx`),
never `**` or `*`. It is read as the activity's claim on the tree, and an
inaccurate claim is what produces the conflict you were asked to avoid.

## Field rules

- `id`: a short, unique, lowercase slug, `[a-z0-9_]` only — `auth_api`,
  `db_schema`. No spaces, no leading or trailing whitespace. This is what
  `depends_on` refers to.
- `title`: one line, imperative, human-readable.
- `description`: the complete brief the agent receives. It is the *only*
  instruction it gets — it never sees the objective, this system prompt, the
  other activities, or their output. Make it self-contained: state the files and
  interfaces involved, what "done" looks like, and any contract another activity
  depends on. Never write "as described above" or "continue from the previous
  step".
- `acceptance_criteria`: verifiable statements, one claim each, checkable by a
  reviewer reading the diff or running one command. Prose is fine — nothing
  executes them automatically — but "it works" is not a criterion.
- `suggested_harness` / `suggested_model`: choose from the catalog given in the
  request. The operator may override both; suggest the cheaper model when the
  activity is mechanical.
- `estimated_effort`: free-text, advisory, purely a badge. Nothing schedules on
  it, so do not encode priority in it.

Return the whole plan. A response is never a patch on a previous one.\
"""

_CORRECTION_RULES = """\
Rules, again: every id in `depends_on` must be the `id` of another activity in \
this same response; ids are unique and have no surrounding whitespace; an \
activity may not depend on itself; and the dependency graph must be acyclic. \
Return the complete corrected plan — every activity, not only the ones named \
above. Keep everything that was already right.\
"""


def objective_prompt(
    objective: str,
    *,
    catalog: Mapping[str, Sequence[str]],
    context: str | None = None,
) -> str:
    """The first user turn: the goal, the repository context, the catalog.

    The harness catalog is rendered from the adapter registry rather than
    written into :data:`SYSTEM_PROMPT`, so installing an adapter changes what
    the planner may suggest without anyone editing a prompt — and so no harness
    name is spelled out in this module (invariant 1 is about conditionals, but a
    hardcoded list would go stale the same way one does).
    """
    lines = [
        "## Objective",
        "",
        objective.strip(),
        "",
        "## Available harnesses",
        "",
    ]
    for harness in sorted(catalog):
        models = ", ".join(catalog[harness]) or "(no model may be named)"
        lines.append(f"- `{harness}` — models: {models}")
    if context:
        lines += ["", "## Repository context", "", context.strip()]
    return "\n".join(lines)


def correction_prompt(invalid: InvalidDag) -> str:
    """The follow-up turn that hands `build_dag`'s verdict back to the model.

    Every defect category at once, because :class:`InvalidDag` reports them in
    one pass and the loop only has three attempts — spending one of them on a
    single typo is how a fixable plan runs out of budget (C2's result section).
    Cycles arrive as a shortest path, so the message says *which edge to delete*
    rather than "there is a cycle somewhere".
    """
    defects = "\n".join(f"- {error.message}" for error in invalid.errors)
    return (
        "The plan you returned is not a valid dependency graph and was "
        "rejected before anything was created. Fix these defects:\n\n"
        f"{defects}\n\n"
        f"{_CORRECTION_RULES}"
    )


# ---------------------------------------------------------------------------
# The response schema (`design.md` §8), as structured output
# ---------------------------------------------------------------------------
#
# Field names are §8's, verbatim, including `suggested_*`: this model is the
# wire contract with the API, and renaming here would hide the mapping onto the
# node table that `to_planned_nodes` performs deliberately.
#
# No `min_length`, `max_length`, `pattern` or numeric bound anywhere below, and
# that is a constraint of the API rather than an oversight: structured output
# does not support them, and the SDK's schema transform moves them into the
# field description and enforces them *client-side* — so a plan that violated
# one would come back as a `pydantic.ValidationError` raised out of `parse()`
# instead of as a defect this module could hand back for correction. Everything
# that must hold about these values is either checked by `build_dag`, repaired
# by `to_planned_nodes`, or stated in the prompt.


class PlannedActivity(BaseModel):
    """One node of the proposal, in the planner's own vocabulary."""

    id: str = Field(
        description=(
            "Unique lowercase slug identifying this activity within this "
            "response, e.g. `auth_api`. Referenced by other activities' "
            "`depends_on`."
        )
    )
    title: str = Field(description="One imperative line describing the activity.")
    description: str = Field(
        description=(
            "The complete, self-contained brief handed to the coding agent. "
            "It sees nothing else."
        )
    )
    depends_on: list[str] = Field(
        description=(
            "Ids of activities that must be merged before this one starts. "
            "Empty when it can start immediately."
        )
    )
    acceptance_criteria: list[str] = Field(
        description="Independently verifiable statements, one claim per entry."
    )
    suggested_harness: str = Field(
        description="A harness name from the catalog in the request."
    )
    suggested_model: str = Field(
        description="A model supported by the chosen harness, from the catalog."
    )
    estimated_effort: str = Field(
        description="Advisory size badge, free text. Nothing schedules on it."
    )
    touches: list[str] = Field(
        description=(
            "Glob patterns this activity will modify. Must not overlap with "
            "activities that can run in parallel with it."
        )
    )


class PlanResponse(BaseModel):
    """What the model returns: a session title and the activities."""

    title: str = Field(description="A short title for the whole plan.")
    nodes: list[PlannedActivity] = Field(
        description="The activities, in any order. Order is expressed by "
        "`depends_on`, never by position."
    )


# ---------------------------------------------------------------------------
# Pure translation and validation
# ---------------------------------------------------------------------------


def harness_catalog() -> dict[str, tuple[str, ...]]:
    """Harness name to supported models, read from the adapter registry.

    The registry is the single source of both (`app.harnesses`), so a catalog
    is data the planner renders and validates against — never a list this
    module maintains.
    """
    return {
        name: tuple(create_adapter(name).supported_models) for name in sorted(ADAPTERS)
    }


def compose_prompt(title: str, description: str) -> str:
    """Fold §8's ``title`` + ``description`` into the node's single ``prompt``.

    `design.md` §8's schema has two fields and the ``node`` table has one
    (``prompt``); ``name`` is taken by the slug, because that is what
    ``depends_on`` and :class:`~app.orchestrator.service.PlannedNode` resolve
    against. Rendering the title as a heading keeps it legible to the agent and
    recoverable by a reader, but it is a fold, not a column — flagged in C8's
    report, because a display title separate from the slug is a migration.
    """
    heading = title.strip()
    body = description.strip()
    if not heading:
        return body
    if not body:
        return f"# {heading}"
    return f"# {heading}\n\n{body}"


def to_planned_nodes(
    response: PlanResponse,
    *,
    catalog: Mapping[str, Sequence[str]],
    fallback_harness: str,
) -> tuple[PlannedNode, ...]:
    """Translate the wire schema into what ``create_graph`` persists.

    Two repairs, both deliberate, neither costing a correction round trip:

    A ``suggested_harness`` with no installed adapter falls back to
    ``fallback_harness``, and a ``suggested_model`` that harness does not
    support becomes ``None`` — "the harness's own default". `design.md` §8 makes
    both of these the *operator's* choice in the editable proposal and does not
    retain what the planner suggested, so failing a whole plan over a value a
    human is about to overwrite would spend an attempt on nothing. Everything
    structural — ids, edges — is left exactly as the model wrote it, because
    :func:`~app.orchestrator.graph.build_dag` must see the defect rather than a
    repair that hides it.
    """
    planned: list[PlannedNode] = []
    for activity in response.nodes:
        harness = (
            activity.suggested_harness
            if activity.suggested_harness in catalog
            else fallback_harness
        )
        model = activity.suggested_model
        supported = catalog.get(harness, ())
        planned.append(
            PlannedNode(
                name=activity.id,
                prompt=compose_prompt(activity.title, activity.description),
                harness=harness,
                model=model if model in supported else None,
                depends_on=tuple(activity.depends_on),
                acceptance_criteria=tuple(activity.acceptance_criteria),
                touches=tuple(activity.touches),
                estimated_effort=activity.estimated_effort.strip() or None,
            )
        )
    return tuple(planned)


def validate_plan(nodes: Sequence[PlannedNode]) -> Dag | InvalidDag:
    """Run the pure core over the planner's slugs, before anything is written.

    ``create_graph`` validates again over allocated ids and would catch the same
    cycle, but two attempts too late and in the wrong vocabulary: by then the
    slug the model can edit has been replaced by a ULID, and the defect is an
    exception at a transport rather than a message the loop can correct.
    """
    return build_dag(
        GraphNode(id=node.name, depends_on=tuple(node.depends_on)) for node in nodes
    )


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


class PlanFailureKind(StrEnum):
    """Why no proposal came back. Every one of these is data, not an exception."""

    INVALID_GRAPH = "invalid_graph"
    """The correction budget ran out with the graph still not a DAG."""

    REFUSED = "refused"
    """``stop_reason: "refusal"`` — a safety classifier declined, with HTTP 200."""

    TRUNCATED = "truncated"
    """The response hit ``max_tokens`` or the context window before finishing."""

    MALFORMED = "malformed"
    """HTTP 200 with content that is not a plan: unparseable, or no activities."""

    API_ERROR = "api_error"
    """The request did not produce a usable response at all."""


@dataclass(frozen=True, slots=True)
class PlannerUsage:
    """The planner's own token consumption — **real spend**, not an estimate.

    Invariant 7's "estimated equivalent" exists because the harnesses run under
    a Max/Pro subscription. This module does not: it bills per token against an
    API key. That is exactly why the number is returned as a value and logged
    rather than written to ``usage_event`` — see :meth:`Planner.propose`.

    ``requests`` counts round trips, so a proposal that took two attempts shows
    what the correction loop cost.
    """

    model: str
    counts: TokenCounts
    cost_usd: float | None
    price_table_version: int
    requests: int = 0

    def with_request(self, counts: TokenCounts, prices: PriceTable) -> PlannerUsage:
        total = self.counts + counts
        return PlannerUsage(
            model=self.model,
            counts=total,
            # Repriced over the running total rather than accumulated per
            # request: identical arithmetic, one rounding path. Computed here,
            # at ingest, with the table in effect now (invariant 3) — never
            # recomputed later from a stored token count.
            cost_usd=prices.cost_usd(self.model, total),
            price_table_version=prices.version,
            requests=self.requests + 1,
        )


@dataclass(frozen=True, slots=True)
class PlanProposal:
    """A validated proposal. Nothing about holding one has executed anything."""

    title: str
    nodes: tuple[PlannedNode, ...]
    usage: PlannerUsage
    attempts: int
    #: The persisted graph. ``None`` from :meth:`Planner.propose`, which does
    #: not touch the database; set by :meth:`Planner.plan_graph`.
    graph: CreatedGraph | None = None


@dataclass(frozen=True, slots=True)
class PlanFailure:
    """No proposal, and why. ``message`` is safe to show an operator."""

    kind: PlanFailureKind
    message: str
    usage: PlannerUsage
    attempts: int
    #: The last verdict from the pure core, for ``INVALID_GRAPH``. C9 renders
    #: the cycle; the message already names it.
    errors: tuple[DagError, ...] = ()


PlanResult = PlanProposal | PlanFailure


class GraphCreator(Protocol):
    """The one write path into the database this module may use.

    Structurally satisfied by
    :meth:`~app.orchestrator.service.NodeRunService.create_graph`. A protocol
    rather than the concrete service so the planner can be tested against a
    graph writer without a repository, a workspace and a harness registry —
    and so it stays impossible for a second path into ``node``/
    ``node_dependency`` to grow here.
    """

    async def create_graph(
        self,
        *,
        repo_path: Path,
        nodes: Sequence[PlannedNode],
        title: str | None = ...,
        auto_merge: bool = ...,
        base_ref: str = ...,
    ) -> CreatedGraph: ...


# ---------------------------------------------------------------------------
# The client shell
# ---------------------------------------------------------------------------


def _token_counts(usage: AnthropicUsage) -> TokenCounts:
    """All four fields of invariant 3, plus the cache-write tier split.

    Summing ``input_tokens`` alone under-reports a cached planning turn by an
    order of magnitude, and collapsing the 5m/1h split misprices cache writes by
    up to 1.6x (`design.md` §4). A split that does not add up to the total is
    dropped rather than trusted: :class:`TokenCounts` rejects it, and a usage
    record must never be able to fail a plan that succeeded.
    """
    creation = usage.cache_creation
    write_total = usage.cache_creation_input_tokens or 0
    write_5m = creation.ephemeral_5m_input_tokens if creation else 0
    write_1h = creation.ephemeral_1h_input_tokens if creation else 0
    if write_5m + write_1h != write_total:
        write_5m = write_1h = 0
    return TokenCounts(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens or 0,
        cache_write_tokens=write_total,
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


def _assistant_text(response: ParsedMessage[PlanResponse]) -> str:
    """The text blocks only.

    Thinking blocks are deliberately not echoed back into the correction turn:
    they are not required outside a tool-use continuation, and a plan is not
    improved by making the model re-read its own reasoning about a graph it got
    wrong.
    """
    return "".join(
        block.text for block in response.content if block.type == "text"
    ).strip()


def _digest(text: str) -> str:
    """Identify a prompt in a log line without logging it (`conventions` §6)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class Planner:
    """Turns an objective into a proposal. Proposes only — invariant 6.

    The client is injected rather than built here so a test can drive the whole
    path, including the SDK's schema transform and response parsing, against a
    recorded response and no network.
    """

    def __init__(
        self,
        *,
        client: AsyncAnthropic,
        settings: Settings,
        prices: PriceTable,
        catalog: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._prices = prices
        self._catalog = dict(harness_catalog() if catalog is None else catalog)
        fallback = settings.planner_fallback_harness
        if fallback not in self._catalog:
            raise ValueError(
                f"planner_fallback_harness {fallback!r} has no adapter; "
                f"available: {sorted(self._catalog)}"
            )
        self._fallback_harness = fallback

    @property
    def catalog(self) -> Mapping[str, Sequence[str]]:
        return self._catalog

    async def propose(
        self, objective: str, *, context: str | None = None
    ) -> PlanResult:
        """Ask for a graph, correct it up to ``planner_max_attempts`` times.

        Persists nothing. The correction loop appends to one conversation — the
        rejected plan as the assistant turn, :func:`correction_prompt` as the
        next user turn — so the model corrects its own text rather than
        re-planning from scratch and losing the parts that were right.
        """
        if not objective.strip():
            raise ValueError("objective must not be empty")

        usage = PlannerUsage(
            model=self._settings.planner_model,
            counts=TokenCounts(),
            cost_usd=None,
            price_table_version=self._prices.version,
        )
        messages: list[MessageParam] = [
            {
                "role": "user",
                "content": objective_prompt(
                    objective, catalog=self._catalog, context=context
                ),
            }
        ]
        bound = log.bind(
            planner_model=self._settings.planner_model,
            objective_sha=_digest(objective),
            objective_chars=len(objective),
        )

        attempt = 0
        invalid: InvalidDag | None = None
        while attempt < self._settings.planner_max_attempts:
            attempt += 1
            try:
                response = await self._request(messages)
            except APIError as exc:
                # The class name and status, never the body or the request: an
                # error rendered into a UI must not become a channel for the
                # credential or the prompt (`docs/conventions.md` §6).
                detail = f"{type(exc).__name__}"
                status = getattr(exc, "status_code", None)
                if status is not None:
                    detail = f"{detail} (HTTP {status})"
                bound.warning("planner.api_error", attempt=attempt, error=detail)
                return PlanFailure(
                    kind=PlanFailureKind.API_ERROR,
                    message=f"the planner API call failed: {detail}",
                    usage=usage,
                    attempts=attempt,
                )
            except ValidationError:
                # `messages.parse` validates the assistant text against the
                # schema inside the SDK, so a response that is JSON-shaped but
                # wrong never reaches `stop_reason`. Reported, not raised.
                bound.warning("planner.unparseable", attempt=attempt)
                return PlanFailure(
                    kind=PlanFailureKind.MALFORMED,
                    message=(
                        "the planner returned content that does not match the "
                        "plan schema"
                    ),
                    usage=usage,
                    attempts=attempt,
                )

            usage = usage.with_request(_token_counts(response.usage), self._prices)

            # Before `content`, always: a refusal is an HTTP 200 whose content
            # is not a plan, and indexing into it is how this breaks in
            # production instead of in a test.
            stop = response.stop_reason
            if stop == "refusal":
                bound.warning("planner.refused", attempt=attempt)
                return PlanFailure(
                    kind=PlanFailureKind.REFUSED,
                    message=(
                        "the planner declined to answer this objective "
                        "(stop_reason: refusal)"
                    ),
                    usage=usage,
                    attempts=attempt,
                )
            if stop in ("max_tokens", "model_context_window_exceeded"):
                bound.warning("planner.truncated", attempt=attempt, stop_reason=stop)
                return PlanFailure(
                    kind=PlanFailureKind.TRUNCATED,
                    message=(
                        f"the planner's response was cut off ({stop}); raise "
                        "AGENTHUB_PLANNER_MAX_TOKENS or narrow the objective"
                    ),
                    usage=usage,
                    attempts=attempt,
                )

            plan = response.parsed_output
            if plan is None or not plan.nodes:
                bound.warning("planner.empty", attempt=attempt, stop_reason=stop)
                return PlanFailure(
                    kind=PlanFailureKind.MALFORMED,
                    message="the planner returned no activities",
                    usage=usage,
                    attempts=attempt,
                )

            nodes = to_planned_nodes(
                plan,
                catalog=self._catalog,
                fallback_harness=self._fallback_harness,
            )
            dag = validate_plan(nodes)
            if isinstance(dag, Dag):
                bound.info(
                    "planner.proposed",
                    attempt=attempt,
                    nodes=len(nodes),
                    requests=usage.requests,
                    tokens=usage.counts.total,
                    cost_usd=usage.cost_usd,
                    price_table_version=usage.price_table_version,
                )
                return PlanProposal(
                    title=plan.title.strip() or objective.strip()[:120],
                    nodes=nodes,
                    usage=usage,
                    attempts=attempt,
                )

            invalid = dag
            bound.warning(
                "planner.invalid_graph",
                attempt=attempt,
                defects=[error.kind.value for error in dag.errors],
                cycles=[list(cycle) for cycle in dag.cycles],
            )
            messages.append({"role": "assistant", "content": _assistant_text(response)})
            messages.append({"role": "user", "content": correction_prompt(dag)})

        errors = invalid.errors if invalid is not None else ()
        bound.error(
            "planner.incorrigible",
            attempts=attempt,
            tokens=usage.counts.total,
            cost_usd=usage.cost_usd,
        )
        return PlanFailure(
            kind=PlanFailureKind.INVALID_GRAPH,
            message=(
                f"the planner did not produce a valid dependency graph in "
                f"{attempt} attempts: " + "; ".join(error.message for error in errors)
            ),
            usage=usage,
            attempts=attempt,
            errors=errors,
        )

    async def plan_graph(
        self,
        objective: str,
        *,
        repo_path: Path,
        creator: GraphCreator,
        context: str | None = None,
        auto_merge: bool = False,
        base_ref: str = "HEAD",
    ) -> PlanResult:
        """:meth:`propose`, then persist the proposal ``pending``.

        Nothing is materialized and nothing runs: ``create_graph`` leaves every
        node ``pending`` with no worktree, which is what invariant 6 requires of
        a proposal a human has not yet approved. On any failure the return is
        the failure and **no row is written** — the validation that would have
        caught it happened before this call.
        """
        result = await self.propose(objective, context=context)
        if isinstance(result, PlanFailure):
            return result
        graph = await creator.create_graph(
            repo_path=repo_path,
            nodes=result.nodes,
            title=result.title,
            auto_merge=auto_merge,
            base_ref=base_ref,
        )
        return replace(result, graph=graph)

    async def _request(
        self, messages: Sequence[MessageParam]
    ) -> ParsedMessage[PlanResponse]:
        """One round trip.

        ``thinking`` is deliberately absent: it is adaptive by default on the
        default model, and pinning it would trade the model's own judgement
        about how much reasoning a graph needs for a constant. ``max_tokens``
        caps thinking *and* the response text together, which is why the
        configured default has headroom over the size of a plan.

        ``effort`` is the depth knob (`output_config`), configured rather than
        constant because a five-node refactor and a thirty-node migration do not
        deserve the same spend.
        """
        effort: PlannerEffort = self._settings.planner_effort
        return await self._client.messages.parse(
            model=self._settings.planner_model,
            max_tokens=self._settings.planner_max_tokens,
            system=SYSTEM_PROMPT,
            messages=list(messages),
            output_format=PlanResponse,
            output_config={"effort": effort},
        )


def create_planner(settings: Settings, prices: PriceTable) -> Planner:
    """A planner on a bare client.

    Bare on purpose: the SDK resolves ``ANTHROPIC_API_KEY``, then
    ``ANTHROPIC_AUTH_TOKEN``, then an ``ant auth login`` profile. Reading the
    variable here and passing it in would break the third of those and would
    put the credential in this process's own reach for no gain —
    `docs/conventions.md` §6 wants fewer places that can hold a secret, not
    more. It is also the only credential AgentHub owns: the harnesses
    authenticate themselves.
    """
    return Planner(client=AsyncAnthropic(), settings=settings, prices=prices)


__all__ = [
    "SYSTEM_PROMPT",
    "GraphCreator",
    "PlanFailure",
    "PlanFailureKind",
    "PlanProposal",
    "PlanResponse",
    "PlanResult",
    "PlannedActivity",
    "Planner",
    "PlannerUsage",
    "compose_prompt",
    "correction_prompt",
    "create_planner",
    "harness_catalog",
    "objective_prompt",
    "to_planned_nodes",
    "validate_plan",
]
