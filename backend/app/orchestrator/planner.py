"""Objective in, validated graph proposal out (`design.md` §8).

**The planner has two interchangeable backends** (`design.md` §8, revised
2026-08-08): :class:`ApiPlanBackend` calls the Anthropic API with
``messages.parse``, and :class:`HarnessPlanBackend` drives any adapter that
implements ``StructuredCompleter`` — Claude Code's ``--json-schema``, Codex's
``--output-schema``. Both return schema-validated content, so neither is
prompting for JSON and parsing prose, which is the thing §8 actually rules out.

The seam is :class:`PlanBackend`, and :meth:`Planner.propose` is written
against it: the correction loop, the DAG validation and the failure vocabulary
below are identical whichever backend is configured. **Nothing in this module
names a harness** — the adapter arrives from :func:`app.harnesses.create_adapter`
by configuration and is asked only whether it *can* return structured content.
Invariant 1 forbids branching on which harness is running, not asking what one
can do.

Which backend runs decides whether the planner is spend. Against the API its
tokens are real per-token billing; against a harness it inherits invariant 7
like everything else, which is what makes the product usable on a subscription
with no API credit at all.

**The choice is per plan, not per server** (`design.md` §8). ``Settings``
supplies the default; a request may carry a :class:`PlannerChoice` naming its
own backend, harness or model, and only the fields it omits fall back. A
thirty-node migration and a one-flag change do not deserve the same model, and
every *node* has always been the operator's choice in the editable proposal —
the planner being the one thing frozen until a restart was an inconsistency.

That splits one failure into two audiences, which is why
:class:`PlannerChoiceError` exists next to :class:`PlanBackendUnavailable`. A
harness named in ``Settings`` that cannot back the planner is the operator's
problem: it stays a logged startup error and a 503 naming the fix. A harness or
model named in a *request* is the caller's: it is refused before anything is
built, with the valid values listed, and reaches them as a 422. Answering 503
to a typo would tell someone the server is broken when their input is.

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
an unreachable backend, a missing credential, a response that will not validate
and an incorrigible cycle are all ordinary outcomes of asking a model for a
graph; every one of them returns a :class:`PlanFailure` carrying enough detail
for C9 to render it. :class:`ValueError` is still raised for programmer error.

The two backends do not have identical failure modes, and none are faked to
pretend otherwise: the API reports a refusal and a truncation through
``stop_reason``, and a CLI has no equivalent, so :attr:`PlanFailureKind.REFUSED`
and :attr:`PlanFailureKind.TRUNCATED` are simply unreachable on that path.

Everything that can be pure is pure and lives at the top of this file
(`docs/architecture.md` §3): the response schema, the translation into
``PlannedNode``, the DAG check and the correction prompt are all plain functions
over plain values, testable with no client, no key and no network.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Literal, Protocol, cast

import structlog
from anthropic import APIError, AsyncAnthropic
from anthropic.types import MessageParam, ParsedMessage
from anthropic.types import Usage as AnthropicUsage
from pydantic import BaseModel, Field, ValidationError

from app.config import PlannerBackendName, PlannerEffort, Settings
from app.harnesses import ADAPTERS, create_adapter
from app.harnesses.base import (
    BaseHarnessAdapter,
    HarnessError,
    StructuredCompleter,
    StructuredRequest,
    supports_structured_output,
)
from app.harnesses.events import Usage
from app.models.pricing import PriceTable, TokenCounts
from app.orchestrator.graph import Dag, DagError, GraphNode, InvalidDag, build_dag
from app.orchestrator.service import CreatedGraph, PlannedNode

log = structlog.get_logger()

# The stable half of the SDK's auth-resolution `TypeError`. Matched rather than
# caught by type because `_validate_headers` raises a bare `TypeError`; the
# leading words have survived the SDK's rewordings of the rest of the sentence.
_NO_CREDENTIAL = "Could not resolve authentication method"


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

    TIMED_OUT = "timed_out"
    """One backend attempt exceeded the configured wall-clock deadline."""

    NOT_CONFIGURED = "not_configured"
    """No credential resolved, so no request was ever made.

    Separate from :attr:`API_ERROR` because nothing failed remotely and a retry
    cannot help: the operator has to supply a credential. It is the one planner
    failure whose fix is on this machine.
    """


class PlanOutcome(StrEnum):
    """What one backend round trip produced, before the DAG is looked at."""

    OK = "ok"
    REFUSED = "refused"
    TRUNCATED = "truncated"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class PlanTurn:
    """One turn of the correction conversation, in neither backend's dialect.

    The API takes `MessageParam`; a CLI takes a prompt on stdin. Neither type
    may be what the loop passes around, or swapping the backend would rewrite
    the loop.
    """

    role: Literal["user", "assistant"]
    text: str


@dataclass(frozen=True, slots=True)
class PlanReply:
    """One backend's answer, normalized.

    ``usage`` is always present and always four fields, even when the backend
    failed: a truncated attempt still burned tokens, and dropping them makes
    the correction loop under-report what it cost.
    """

    outcome: PlanOutcome
    usage: TokenCounts
    plan: PlanResponse | None = None
    # What actually answered. Empty when the backend could not tell, which
    # leaves the configured label in place rather than inventing one.
    model: str = ""
    # Free text for the outcome that has more than one cause — which stop
    # reason truncated it. Rendered into the failure message, never logged raw.
    detail: str = ""


class PlanBackendUnavailable(Exception):
    """The backend cannot run at all, and no request was attempted.

    A missing credential, or a harness that does not do structured output.
    Distinct from a failed request: nothing was spent and a retry cannot help
    until a human changes the configuration.
    """


class PlanBackendError(Exception):
    """A request was attempted and did not come back usable."""


#: Whether each backend's tokens are billed per token (invariant 7). Constants
#: rather than two literals, because the backend answers this at run time and
#: :func:`planner_options` has to answer the same question *before* a backend
#: exists — a UI that labelled an `api` plan "estimated equivalent" would be
#: promising free what is not, and the two answers drifting is how that
#: happens.
API_IS_SPEND = True
HARNESS_IS_SPEND = False


class PlanBackend(Protocol):
    """Where a plan comes from. `design.md` §8's backend seam.

    Two implementations: the Anthropic API, and any harness adapter that
    implements `StructuredCompleter`. The loop in :meth:`Planner.propose` is
    written against this and knows about neither.
    """

    @property
    def model(self) -> str: ...

    @property
    def is_spend(self) -> bool:
        """Whether this backend's tokens are billed, or ride a subscription.

        Invariant 7 lives or dies on this being read rather than assumed: the
        two backends give the same number two different meanings.
        """
        ...

    async def request(self, turns: Sequence[PlanTurn]) -> PlanReply: ...

    async def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class PlannerUsage:
    """The planner's own token consumption.

    ``is_spend`` is the whole reason this is not just a number. On the ``api``
    backend the planner bills per token against a credential of its own, so
    ``cost_usd`` is **real money**. On the ``harness`` backend it rides an
    already-paid subscription, so the same field is invariant 7's *estimated
    equivalent* and calling it spend would be a lie. Read the flag; never
    assume, and never label the value without it.

    Either way it is returned as a value and logged rather than written to
    ``usage_event`` — a plan is not a node, and that table's ``run_id``,
    ``session_id`` and ``harness`` are all ``NOT NULL``. See
    :meth:`Planner.propose`.

    ``model`` is what actually answered, which on the harness backend is
    whatever the CLI chose and not ``planner_model``. It is what ``cost_usd``
    is priced against, so a wrong label here is a wrong number.

    ``requests`` counts round trips, so a proposal that took two attempts shows
    what the correction loop cost.
    """

    model: str
    counts: TokenCounts
    cost_usd: float | None
    price_table_version: int
    requests: int = 0
    is_spend: bool = True

    def with_request(
        self, counts: TokenCounts, prices: PriceTable, *, model: str = ""
    ) -> PlannerUsage:
        total = self.counts + counts
        # The backend reports what actually answered; the configured label is
        # only a placeholder until the first reply arrives.
        priced_as = model or self.model
        return PlannerUsage(
            model=priced_as,
            counts=total,
            # Repriced over the running total rather than accumulated per
            # request: identical arithmetic, one rounding path. Computed here,
            # at ingest, with the table in effect now (invariant 3) — never
            # recomputed later from a stored token count.
            cost_usd=prices.cost_usd(priced_as, total),
            price_table_version=prices.version,
            requests=self.requests + 1,
            is_spend=self.is_spend,
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

    async def validate_repo(self, repo_path: Path, *, base_ref: str = ...) -> Path: ...

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


def _assistant_text(plan: PlanResponse) -> str:
    """The rejected plan, as the assistant turn of the correction conversation.

    The *parsed* plan and not the raw assistant text, for two reasons. It is
    the only form both backends have — a CLI returns a structured object and no
    content blocks — and it is canonical, so the model re-reads the graph it
    actually produced rather than whatever prose surrounded it.

    Thinking is deliberately not echoed back: it is not required outside a
    tool-use continuation, and a plan is not improved by making the model
    re-read its own reasoning about a graph it got wrong.
    """
    return plan.model_dump_json(indent=2)


def _digest(text: str) -> str:
    """Identify a prompt in a log line without logging it (`conventions` §6)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _strict_schema(schema: dict[str, object]) -> dict[str, object]:
    """Make every object closed, and verify it was already exhaustive.

    Codex rejects a schema whose objects omit ``additionalProperties: false``
    or whose ``required`` does not list every key in ``properties``
    (``invalid_json_schema``, exit 1). Claude Code accepts both the strict and
    the loose form, so the strict one is simply the portable dialect — this is
    a tightening that both adapters can take, not a branch on which harness is
    running, which invariant 1 would forbid.

    ``additionalProperties`` is *added*, because closing an object cannot
    change what a valid document means. ``required`` is only *checked*: filling
    it in would silently promote an optional field to mandatory and change what
    the model is being asked for. `PlanResponse` happens to have no optional
    fields today, and if someone adds one this raises here — at construction,
    where `create_backend` turns it into a logged startup error and a 503 —
    instead of as a Codex ``invalid_json_schema`` on somebody's first objective.
    """

    def tighten(node: object, path: str) -> object:
        if isinstance(node, list):
            return [tighten(item, f"{path}[]") for item in node]
        if not isinstance(node, dict):
            return node
        walked = {key: tighten(value, f"{path}.{key}") for key, value in node.items()}
        if walked.get("type") != "object":
            return walked
        properties = walked.get("properties")
        if isinstance(properties, dict):
            required = walked.get("required")
            listed = set(required) if isinstance(required, list) else set()
            missing = sorted(set(properties) - listed)
            if missing:
                raise ValueError(
                    f"plan schema is not strict at {path or 'the root'}: "
                    f"{missing} are in `properties` but not in `required`. "
                    "Codex rejects that. Make the field required, or give the "
                    "adapters an explicitly nullable one."
                )
        return {**walked, "additionalProperties": False}

    tightened = tighten(schema, "")
    assert isinstance(tightened, dict)
    return tightened


def _flatten_schema(schema: dict[str, object]) -> dict[str, object]:
    """Inline every ``$ref`` and drop ``$defs``.

    Pydantic hoists nested models into ``$defs`` and points at them with
    ``$ref``. `StructuredRequest` promises adapters a resolved schema because
    the two CLIs disagree about supporting references, and resolving it once
    here beats each adapter carrying its own copy of this.

    The plan schema is a shallow tree with no recursion, so a plain expansion
    terminates. A self-referential model would not, and there is no reason for
    the planner to grow one.
    """
    defs = schema.get("$defs")
    definitions: Mapping[str, object] = defs if isinstance(defs, dict) else {}

    def expand(node: object) -> object:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                name = ref.rsplit("/", 1)[-1]
                target = definitions.get(name)
                if target is None:
                    raise ValueError(f"unresolvable $ref in plan schema: {ref}")
                # Sibling keys next to a `$ref` (a `description`, say) survive
                # the expansion; dropping them would lose field documentation
                # the model is meant to read.
                merged = {k: v for k, v in node.items() if k != "$ref"}
                expanded = expand(target)
                assert isinstance(expanded, dict)
                return {**expanded, **merged}
            return {key: expand(value) for key, value in node.items() if key != "$defs"}
        if isinstance(node, list):
            return [expand(item) for item in node]
        return node

    resolved = expand(schema)
    assert isinstance(resolved, dict)
    return resolved


class ApiPlanBackend:
    """`messages.parse` against the Anthropic API. Real per-token spend.

    The client is injected rather than built here so a test can drive the whole
    path, including the SDK's schema transform and response parsing, against a
    recorded response and no network.
    """

    def __init__(self, *, client: AsyncAnthropic, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def model(self) -> str:
        return self._settings.planner_model

    @property
    def is_spend(self) -> bool:
        """Real per-token billing against a credential of the planner's own."""
        return API_IS_SPEND

    async def close(self) -> None:
        await self._client.close()

    async def request(self, turns: Sequence[PlanTurn]) -> PlanReply:
        """One round trip.

        ``thinking`` is deliberately absent: it is adaptive by default on the
        default model, and pinning it would trade the model's own judgement
        about how much reasoning a graph needs for a constant. ``max_tokens``
        caps thinking *and* the response text together, which is why the
        configured default has headroom over the size of a plan.

        ``effort`` is the depth knob (`output_config`), configured rather than
        constant because a five-node refactor and a thirty-node migration do
        not deserve the same spend.
        """
        effort: PlannerEffort = self._settings.planner_effort
        messages: list[MessageParam] = [
            {"role": turn.role, "content": turn.text} for turn in turns
        ]
        try:
            response: ParsedMessage[PlanResponse] = await self._client.messages.parse(
                model=self._settings.planner_model,
                max_tokens=self._settings.planner_max_tokens,
                system=SYSTEM_PROMPT,
                messages=messages,
                output_format=PlanResponse,
                output_config={"effort": effort},
            )
        except TypeError as exc:
            # The SDK's own verdict, after it has tried `ANTHROPIC_API_KEY`,
            # `ANTHROPIC_AUTH_TOKEN` and the `ant auth login` profile — see
            # `create_planner` on why this module deliberately checks none of
            # them itself. It arrives as a bare `TypeError` from a private
            # `_validate_headers` hook, so the message is the only thing that
            # distinguishes it; anything else with this type is a real bug and
            # must keep propagating.
            if _NO_CREDENTIAL not in str(exc):
                raise
            raise PlanBackendUnavailable(
                "the planner has no Anthropic credential: set ANTHROPIC_API_KEY "
                "(or ANTHROPIC_AUTH_TOKEN) in the environment that starts the "
                "server and restart it, or point AGENTHUB_PLANNER_BACKEND at a "
                "harness, which authenticates itself. Note that a Claude "
                "Max/Pro plan is not API access and `ant auth login` is "
                "organization auth, not a route from one."
            ) from exc
        except APIError as exc:
            # The class name and status, never the body or the request: an
            # error rendered into a UI must not become a channel for the
            # credential or the prompt (`docs/conventions.md` §6).
            detail = type(exc).__name__
            status = getattr(exc, "status_code", None)
            if status is not None:
                detail = f"{detail} (HTTP {status})"
            raise PlanBackendError(detail) from exc
        except ValidationError:
            # `messages.parse` validates the assistant text against the schema
            # inside the SDK, so a response that is JSON-shaped but wrong never
            # reaches `stop_reason`. Malformed and not an error: the call
            # succeeded and the model answered — what came back is unusable.
            #
            # The tokens are lost with it, because the exception carries no
            # usage. That is the SDK's shape, not a choice here, and it is why
            # this is the one path that reports zeros for a round trip that
            # really happened.
            return PlanReply(outcome=PlanOutcome.MALFORMED, usage=TokenCounts())

        usage = _token_counts(response.usage)

        # Before `content`, always: a refusal is an HTTP 200 whose content is
        # not a plan, and indexing into it is how this breaks in production
        # instead of in a test.
        stop = response.stop_reason
        model = response.model or self._settings.planner_model
        if stop == "refusal":
            return PlanReply(
                outcome=PlanOutcome.REFUSED,
                usage=usage,
                model=model,
                detail=f"stop_reason: {stop}",
            )
        if stop in ("max_tokens", "model_context_window_exceeded"):
            return PlanReply(
                outcome=PlanOutcome.TRUNCATED,
                usage=usage,
                model=model,
                detail=str(stop),
            )
        return PlanReply(
            outcome=PlanOutcome.OK,
            usage=usage,
            model=model,
            plan=response.parsed_output,
        )


class HarnessPlanBackend:
    """A harness adapter that can return schema-validated content.

    This is the backend that makes the planner usable on a subscription: the
    CLI is already authenticated, so the planner stops being the one component
    with real per-token billing and inherits invariant 7 like everything else.

    Nothing here names a harness. The adapter is resolved by
    `app.harnesses.create_adapter` from configuration and asked only whether it
    has the capability — invariant 1 forbids branching on which one it is, not
    asking what it can do.
    """

    def __init__(
        self,
        *,
        adapter: BaseHarnessAdapter,
        settings: Settings,
    ) -> None:
        if not supports_structured_output(adapter):
            raise PlanBackendUnavailable(
                f"harness {adapter.name!r} cannot return schema-validated "
                "content, so it cannot back the planner. Set "
                "AGENTHUB_PLANNER_BACKEND to `api`, or name a harness that can."
            )
        model = settings.planner_harness_model
        if model is not None and model not in adapter.supported_models:
            raise PlanBackendUnavailable(
                f"planner_harness_model {model!r} is not one of "
                f"{adapter.name!r}'s models: {adapter.supported_models!r}"
            )
        self._adapter = adapter
        self._completer = cast(StructuredCompleter, adapter)
        self._settings = settings
        self._model = model
        self._schema = _strict_schema(_flatten_schema(PlanResponse.model_json_schema()))

    @property
    def model(self) -> str:
        # The adapter decides when nothing is configured, and it reports what
        # it actually used on the reply; this is only the label for logging
        # before the first round trip.
        return self._model or f"{self._adapter.name}:default"

    @property
    def is_spend(self) -> bool:
        """False: the CLI is already authenticated under a subscription.

        This is what makes the planner obey invariant 7 like every node — the
        tokens are real, the money was already paid, and the cost is an
        estimated equivalent.
        """
        return HARNESS_IS_SPEND

    async def close(self) -> None:
        """Nothing to release: each request is its own process."""

    async def request(self, turns: Sequence[PlanTurn]) -> PlanReply:
        """One CLI invocation.

        A CLI has no conversation to append to across processes, so the
        correction loop's turns are rendered into a single prompt. That is a
        real difference from the API backend and not a workaround: the loop's
        contract is "the model sees what it said and why it was rejected",
        which a transcript satisfies.
        """
        try:
            result = await self._completer.complete_structured(
                StructuredRequest(
                    prompt=_render_transcript(turns),
                    schema=self._schema,
                    system=SYSTEM_PROMPT,
                    model=self._model,
                )
            )
        except HarnessError as exc:
            # Structured-completion errors are deliberately safe for display:
            # adapters omit prompts and answers, bound provider diagnostics,
            # and keep stderr in logs. Reducing that to only ``HarnessError``
            # made subscription failures impossible to act on from the UI.
            detail = str(exc).strip() or type(exc).__name__
            raise PlanBackendError(detail) from exc

        usage = _counts_from_usage(result.usage)
        try:
            plan = PlanResponse.model_validate(result.data)
        except ValidationError:
            # The harness validated against the JSON Schema; this validates
            # against the Pydantic model, which is stricter. A gap between them
            # is malformed content, not a broken harness.
            return PlanReply(
                outcome=PlanOutcome.MALFORMED, usage=usage, model=result.model
            )
        return PlanReply(
            outcome=PlanOutcome.OK, usage=usage, model=result.model, plan=plan
        )


def _render_transcript(turns: Sequence[PlanTurn]) -> str:
    """The correction conversation as one prompt.

    Headed rather than run together: the rejected plan and the correction have
    to stay distinguishable, or the model reads its own previous answer as part
    of the instruction.
    """
    if len(turns) == 1:
        return turns[0].text
    blocks = [
        f"## {'Your previous answer' if turn.role == 'assistant' else 'Instruction'}"
        f"\n\n{turn.text}"
        for turn in turns
    ]
    return "\n\n".join(blocks)


def _counts_from_usage(usage: Usage | None) -> TokenCounts:
    """A harness `Usage` event as the planner's four fields.

    ``None`` becomes zeros rather than an error: a plan that succeeded must not
    be failed by an accounting gap, which is the same rule `_token_counts`
    follows for a split that does not add up. The adapter's `ParseStats`
    already records the gap for anyone auditing it.
    """
    if usage is None:
        return TokenCounts()
    return TokenCounts(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cache_write_5m_tokens=usage.cache_write_5m_tokens,
        cache_write_1h_tokens=usage.cache_write_1h_tokens,
    )


# ---------------------------------------------------------------------------
# Choosing a backend per plan (`design.md` §8)
# ---------------------------------------------------------------------------


class PlannerChoiceError(ValueError):
    """A *request* named a backend, harness or model that cannot be used.

    The same shape of problem as :class:`PlanBackendUnavailable` and a
    different audience, which is the whole reason both exist. A harness that
    cannot back the planner is a **configuration** error when ``Settings``
    names it — the operator who set it is the one who can fix it, so it stays a
    logged startup error and a 503 — and a **request** error when a body names
    it, where telling the person who typed it that the server is unavailable
    sends them looking in the wrong place.

    A ``ValueError`` so :func:`app.api.deps.call` already translates it to 422
    without the transport learning a second error vocabulary. The message lists
    what would have been valid; it never carries the objective or a credential
    (`docs/conventions.md` §6).
    """


@dataclass(frozen=True, slots=True)
class PlannerChoice:
    """One plan's backend, harness and model. Every field optional.

    An absent field falls back to ``Settings``, so a caller that only wants a
    cheaper model for one plan does not have to restate the backend it is
    already on, and a caller that wants none of this sends nothing.
    """

    backend: PlannerBackendName | None = None
    harness: str | None = None
    model: str | None = None

    def is_empty(self) -> bool:
        """True when this asks for nothing ``Settings`` does not already say.

        Such a choice is answered by the application's own long-lived backend
        rather than by a temporary copy of it: building one to immediately
        close it would open and drop an HTTP client per plan for no difference
        in behaviour.
        """
        return self.backend is None and self.harness is None and self.model is None


@dataclass(frozen=True, slots=True)
class PlannerOption:
    """One selectable backend, and what is true about choosing it.

    ``supports_effort`` is False for every harness, and that is not a gap to be
    filled later: ``planner_effort`` and ``planner_max_tokens`` are
    ``output_config`` on an API request, and a CLI decides its own depth. A UI
    that offered an effort control beside a harness would be showing a knob
    connected to nothing.
    """

    backend: PlannerBackendName
    #: ``None`` on the `api` backend, which runs no harness at all.
    harness: str | None
    models: tuple[str, ...]
    is_spend: bool
    supports_effort: bool


@dataclass(frozen=True, slots=True)
class PlannerDefault:
    """What a plan request that names nothing resolves to.

    ``selectable`` says whether that default is also one of the offered
    options — it is not, when ``Settings`` names a harness with no adapter or a
    model that harness does not have. It is a **structural** answer, about the
    registry and the model lists, and never a guess about credentials: see
    :func:`planner_options`.
    """

    backend: PlannerBackendName
    harness: str | None
    model: str | None
    selectable: bool


@dataclass(frozen=True, slots=True)
class PlannerOptions:
    """Everything a client needs to render the choice honestly."""

    default: PlannerDefault
    options: tuple[PlannerOption, ...]


def selectable_harnesses() -> dict[str, tuple[str, ...]]:
    """Installed adapters that can back the planner, and their models.

    Filtered by :func:`~app.harnesses.base.supports_structured_output` over the
    registry, never by a list kept here: an adapter that grows the capability
    appears on its own, one that never has it simply does not appear, and
    nothing above ``harnesses/`` compares a name to a literal (invariant 1).
    """
    catalog: dict[str, tuple[str, ...]] = {}
    for name in sorted(ADAPTERS):
        adapter = create_adapter(name)
        if supports_structured_output(adapter):
            catalog[name] = tuple(adapter.supported_models)
    return catalog


def planner_options(settings: Settings) -> PlannerOptions:
    """The selectable backends, and the default a choice-less request gets.

    The `api` backend is **always** listed and is never probed for a
    credential. The SDK resolves three sources — ``ANTHROPIC_API_KEY``,
    ``ANTHROPIC_AUTH_TOKEN``, then an ``ant auth login`` profile — and the
    third is not inspectable from here, so any "available" flag would be a
    guess that is wrong exactly when it matters. A missing credential already
    surfaces on use as :attr:`PlanFailureKind.NOT_CONFIGURED`, a 503 naming the
    fix, which is a better answer than a greyed-out option nobody can explain.
    """
    selectable = selectable_harnesses()
    options = [
        PlannerOption(
            backend="api",
            harness=None,
            models=tuple(settings.planner_api_models),
            is_spend=API_IS_SPEND,
            # The one backend whose depth we set: `output_config` is an API
            # request field, and no CLI takes one.
            supports_effort=True,
        ),
        *(
            PlannerOption(
                backend="harness",
                harness=name,
                models=models,
                is_spend=HARNESS_IS_SPEND,
                supports_effort=False,
            )
            for name, models in selectable.items()
        ),
    ]

    if settings.planner_backend == "api":
        default = PlannerDefault(
            backend="api",
            harness=None,
            model=settings.planner_model,
            selectable=settings.planner_model in settings.planner_api_models,
        )
    else:
        harness = settings.planner_harness
        model = settings.planner_harness_model
        models = selectable.get(harness)
        default = PlannerDefault(
            backend="harness",
            harness=harness,
            model=model,
            # `None` is a real value here — "whatever the CLI is configured
            # for" — and is selectable whenever the harness itself is.
            selectable=models is not None and (model is None or model in models),
        )
    return PlannerOptions(default=default, options=tuple(options))


class Planner:
    """Turns an objective into a proposal. Proposes only — invariant 6.

    The backend is injected rather than built here so a test can drive the
    whole path — the correction loop, the DAG validation, the persistence —
    against a recorded response and no network, and so `design.md` §8's two
    backends are interchangeable at this seam rather than behind a flag inside
    it.
    """

    def __init__(
        self,
        *,
        backend: PlanBackend,
        settings: Settings,
        prices: PriceTable,
        catalog: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        self._backend = backend
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

    def options(self) -> PlannerOptions:
        """What a plan may choose, and what choosing nothing gets.

        Read from the planner's own settings, so what this advertises is what
        :meth:`propose` will actually resolve.
        """
        return planner_options(self._settings)

    async def close(self) -> None:
        """Release whatever the backend owns, on behalf of the composition root."""
        await self._backend.close()

    async def propose(
        self,
        objective: str,
        *,
        context: str | None = None,
        choice: PlannerChoice | None = None,
    ) -> PlanResult:
        """Ask for a graph, correct it up to ``planner_max_attempts`` times.

        With no ``choice`` this runs on the long-lived backend the application
        owns and **does not close it** — it belongs to the composition root,
        and closing it here would break every plan after the first. A choice
        gets a backend of its own for the duration of the call, closed in a
        ``finally`` because the API backend holds an ``httpx`` client and one
        left open per plan is a real leak.

        A choice that names something unusable raises
        :class:`PlannerChoiceError` **before** a backend is built: nothing is
        spent, and the caller hears that their input was wrong rather than that
        the planner is unavailable.
        """
        if choice is None or choice.is_empty():
            return await self._propose(
                objective, context=context, backend=self._backend
            )
        backend = create_backend(self._settings, choice)
        try:
            return await self._propose(objective, context=context, backend=backend)
        finally:
            await backend.close()

    async def _propose(
        self, objective: str, *, context: str | None, backend: PlanBackend
    ) -> PlanResult:
        """The correction loop, over whichever backend this plan resolved to.

        Persists nothing. It appends to one conversation — the rejected plan as
        the assistant turn, :func:`correction_prompt` as the next user turn —
        so the model corrects its own text rather than re-planning from scratch
        and losing the parts that were right.
        """
        if not objective.strip():
            raise ValueError("objective must not be empty")

        usage = PlannerUsage(
            # A placeholder until the first reply says what actually answered:
            # the harness backend does not use `planner_model` at all.
            model=backend.model,
            counts=TokenCounts(),
            cost_usd=None,
            # Read from whichever backend this plan resolved to, so a
            # per-request `api` choice is labelled spend and a per-request
            # harness choice is invariant 7's estimated equivalent.
            is_spend=backend.is_spend,
            price_table_version=self._prices.version,
        )
        messages: list[PlanTurn] = [
            PlanTurn(
                role="user",
                text=objective_prompt(
                    objective, catalog=self._catalog, context=context
                ),
            )
        ]
        bound = log.bind(
            planner_model=backend.model,
            objective_sha=_digest(objective),
            objective_chars=len(objective),
        )

        attempt = 0
        invalid: InvalidDag | None = None
        while attempt < self._settings.planner_max_attempts:
            attempt += 1
            try:
                async with asyncio.timeout(self._settings.planner_timeout_s):
                    reply = await backend.request(messages)
            except TimeoutError:
                seconds = self._settings.planner_timeout_s
                bound.warning(
                    "planner.timed_out", attempt=attempt, timeout_seconds=seconds
                )
                return PlanFailure(
                    kind=PlanFailureKind.TIMED_OUT,
                    message=(
                        f"the planner request exceeded {seconds:g} seconds; "
                        "retry, choose another model, or raise "
                        "AGENTHUB_PLANNER_TIMEOUT_S"
                    ),
                    usage=usage,
                    attempts=attempt,
                )
            except PlanBackendUnavailable as exc:
                # Nothing was attempted and nothing was spent. The fix is on
                # this machine, which is why it is not `API_ERROR`.
                bound.warning("planner.not_configured", attempt=attempt)
                return PlanFailure(
                    kind=PlanFailureKind.NOT_CONFIGURED,
                    message=str(exc),
                    usage=usage,
                    attempts=attempt,
                )
            except PlanBackendError as exc:
                # The backend already reduced this to a safe label — a class
                # name and maybe a status. An error rendered into a UI must not
                # become a channel for the credential or the prompt
                # (`docs/conventions.md` §6), so it is not re-inspected here.
                detail = str(exc)
                bound.warning("planner.api_error", attempt=attempt, error=detail)
                return PlanFailure(
                    kind=PlanFailureKind.API_ERROR,
                    message=f"the planner request failed: {detail}",
                    usage=usage,
                    attempts=attempt,
                )

            usage = usage.with_request(reply.usage, self._prices, model=reply.model)

            # Before the plan, always: a refusal and a truncation both arrive
            # as a successful call whose content is not a plan, and indexing
            # into it is how this breaks in production instead of in a test.
            if reply.outcome is PlanOutcome.REFUSED:
                bound.warning("planner.refused", attempt=attempt)
                # The backend appends why when it knows: the API says
                # `stop_reason: refusal`, a CLI has no equivalent to report.
                because = f" ({reply.detail})" if reply.detail else ""
                return PlanFailure(
                    kind=PlanFailureKind.REFUSED,
                    message=(f"the planner declined to answer this objective{because}"),
                    usage=usage,
                    attempts=attempt,
                )
            if reply.outcome is PlanOutcome.TRUNCATED:
                bound.warning(
                    "planner.truncated", attempt=attempt, stop_reason=reply.detail
                )
                return PlanFailure(
                    kind=PlanFailureKind.TRUNCATED,
                    message=(
                        f"the planner's response was cut off ({reply.detail}); "
                        "raise AGENTHUB_PLANNER_MAX_TOKENS or narrow the objective"
                    ),
                    usage=usage,
                    attempts=attempt,
                )
            if reply.outcome is PlanOutcome.MALFORMED:
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

            plan = reply.plan
            if plan is None or not plan.nodes:
                bound.warning("planner.empty", attempt=attempt)
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
            messages.append(PlanTurn(role="assistant", text=_assistant_text(plan)))
            messages.append(PlanTurn(role="user", text=correction_prompt(dag)))

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
        choice: PlannerChoice | None = None,
        auto_merge: bool = False,
        base_ref: str = "HEAD",
    ) -> PlanResult:
        """:meth:`propose`, then persist the proposal ``pending``.

        The repository and base ref are preflighted before :meth:`propose`.
        Planning may spend tokens or occupy a subscription turn, and doing so
        for a path that can never host invariant 2's worktrees is pure waste.
        Creation validates them again after the model returns because the
        repository can change in between.

        Nothing is materialized and nothing runs: ``create_graph`` leaves every
        node ``pending`` with no worktree, which is what invariant 6 requires of
        a proposal a human has not yet approved. On any failure the return is
        the failure and **no row is written** — the validation that would have
        caught it happened before this call.

        ``choice`` behaves exactly as in :meth:`propose`, including raising
        :class:`PlannerChoiceError` before anything is asked of a model. A
        rejected choice therefore also writes nothing.
        """
        await creator.validate_repo(repo_path, base_ref=base_ref)
        result = await self.propose(objective, context=context, choice=choice)
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


class UnavailablePlanBackend:
    """A backend that reports, on use, why it could not be built.

    The planner is one feature of five. A harness that cannot do structured
    output, or a `planner_harness` naming no adapter at all, must not stop the
    scheduler, the dashboards and code search from starting — on a
    single-user local tool, refusing to boot over a misconfigured planner takes
    away four working things to punish one broken one.

    So the failure is deferred rather than swallowed: it is logged once at
    startup, where whoever set the configuration will see it, and returned as
    :attr:`PlanFailureKind.NOT_CONFIGURED` — a 503 naming the fix — to whoever
    later asks for a plan.
    """

    def __init__(self, reason: str) -> None:
        self._reason = reason

    @property
    def model(self) -> str:
        return "unavailable"

    @property
    def is_spend(self) -> bool:
        """Nothing runs, so nothing is billed either way."""
        return False

    async def close(self) -> None:
        """Nothing was built, so there is nothing to release."""

    async def request(self, turns: Sequence[PlanTurn]) -> PlanReply:
        raise PlanBackendUnavailable(self._reason)


def _resolve_choice(settings: Settings, choice: PlannerChoice) -> Settings:
    """Fold a request's choice into a copy of ``settings``, or refuse it.

    Returning settings rather than a backend is what keeps both constructors
    exactly as they were: there is still one configuration object that a
    backend reads, and no second path by which a harness or a model reaches
    one.

    Only what the *request* named is validated here, and that asymmetry is the
    point. A model the request supplied is checked against the harness it will
    actually run on, so a typo is a 422 listing that harness's models. A
    harness the *settings* named is left alone even when it is broken, so it
    keeps degrading through :class:`UnavailablePlanBackend` into the 503 that
    tells the operator to fix their configuration.
    """
    backend = choice.backend or settings.planner_backend
    if backend not in ("harness", "api"):
        raise PlannerChoiceError(
            f"unknown planner backend {backend!r}; valid: ['api', 'harness']"
        )

    update: dict[str, object] = {"planner_backend": backend}
    if backend == "api":
        if choice.harness is not None:
            raise PlannerChoiceError(
                "the `api` planner backend runs no harness, so it cannot be "
                f"combined with harness {choice.harness!r}; drop the harness, "
                "or choose the `harness` backend"
            )
        if choice.model is not None:
            if choice.model not in settings.planner_api_models:
                raise PlannerChoiceError(
                    f"model {choice.model!r} is not selectable for the `api` "
                    f"planner backend; valid: {list(settings.planner_api_models)}"
                )
            update["planner_model"] = choice.model
        return settings.model_copy(update=update)

    selectable = selectable_harnesses()
    harness = choice.harness or settings.planner_harness
    if choice.harness is not None:
        if harness not in ADAPTERS:
            raise PlannerChoiceError(
                f"unknown harness {harness!r}; the planner can use: "
                f"{sorted(selectable)}"
            )
        if harness not in selectable:
            raise PlannerChoiceError(
                f"harness {harness!r} cannot return schema-validated content, "
                f"so it cannot back the planner; the planner can use: "
                f"{sorted(selectable)}"
            )
    model = choice.model
    if model is not None and harness in selectable and model not in selectable[harness]:
        raise PlannerChoiceError(
            f"model {model!r} is not one of harness {harness!r}'s models; "
            f"valid: {list(selectable[harness])}"
        )
    if model is None:
        # A pinned `planner_harness_model` belongs to the harness it was pinned
        # for. Carrying it onto a *different* one the request asked for would
        # turn a valid request into a 503 about a model nobody named here.
        model = settings.planner_harness_model
        if choice.harness is not None and model not in selectable.get(harness, ()):
            model = None
    update["planner_harness"] = harness
    update["planner_harness_model"] = model
    return settings.model_copy(update=update)


def create_backend(
    settings: Settings, choice: PlannerChoice | None = None
) -> PlanBackend:
    """The configured backend, or one that explains why there isn't one.

    The API client is built bare on purpose: the SDK resolves
    ``ANTHROPIC_API_KEY``, then ``ANTHROPIC_AUTH_TOKEN``, then an ``ant auth
    login`` profile. Reading the variable here and passing it in would break
    the third of those and would put the credential in this process's own reach
    for no gain — `docs/conventions.md` §6 wants fewer places that can hold a
    secret, not more. It never fails here: a missing credential is only
    discovered on the first request, because only the SDK knows whether one of
    those three sources answered.

    The harness backend *can* fail here, and that failure is captured into
    :class:`UnavailablePlanBackend` rather than raised.

    ``choice`` is one request's override of `design.md` §8's three settings.
    Anything it names is validated first and refused with
    :class:`PlannerChoiceError`, which does **not** degrade: the deferral above
    exists so a misconfigured server still boots its other four features, and
    it is the wrong answer to a body somebody just typed. With no choice — or
    one that names nothing — the behaviour below is exactly what it was.
    """
    resolved = (
        settings
        if choice is None or choice.is_empty()
        else _resolve_choice(settings, choice)
    )
    if resolved.planner_backend == "api":
        return ApiPlanBackend(client=AsyncAnthropic(), settings=resolved)
    try:
        adapter = create_adapter(resolved.planner_harness)
        return HarnessPlanBackend(adapter=adapter, settings=resolved)
    except (PlanBackendUnavailable, ValueError) as exc:
        # ValueError is `create_adapter` on a harness name that has no adapter.
        log.error(
            "planner.backend_unavailable",
            planner_backend=resolved.planner_backend,
            planner_harness=resolved.planner_harness,
            error=str(exc),
        )
        return UnavailablePlanBackend(str(exc))


def create_planner(settings: Settings, prices: PriceTable) -> Planner:
    """A planner on the configured backend."""
    return Planner(backend=create_backend(settings), settings=settings, prices=prices)


__all__ = [
    "API_IS_SPEND",
    "HARNESS_IS_SPEND",
    "SYSTEM_PROMPT",
    "ApiPlanBackend",
    "GraphCreator",
    "HarnessPlanBackend",
    "PlanBackend",
    "PlanBackendError",
    "PlanBackendUnavailable",
    "PlanFailure",
    "PlanFailureKind",
    "PlanOutcome",
    "PlanProposal",
    "PlanReply",
    "PlanResponse",
    "PlanResult",
    "PlanTurn",
    "PlannedActivity",
    "Planner",
    "PlannerChoice",
    "PlannerChoiceError",
    "PlannerDefault",
    "PlannerOption",
    "PlannerOptions",
    "PlannerUsage",
    "UnavailablePlanBackend",
    "compose_prompt",
    "correction_prompt",
    "create_backend",
    "create_planner",
    "harness_catalog",
    "objective_prompt",
    "planner_options",
    "selectable_harnesses",
    "to_planned_nodes",
    "validate_plan",
]
