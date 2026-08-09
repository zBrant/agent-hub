"""The one ``Settings`` object (`docs/conventions.md` §2).

Read once, at a boundary. No ``os.getenv()`` anywhere else, and the defaults
have to work on a clean machine with no ``.env`` — ``uv run fastapi dev`` must
not require setup.

The runtime root is ``~/.agenthub/`` (`docs/architecture.md` §4) and everything
under it derives from that one value, so a test overrides ``AGENTHUB_ROOT`` (or
constructs ``Settings(root=tmp_path)``) and is guaranteed not to touch the real
database, run logs, or workspaces.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ROOT = Path.home() / ".agenthub"
# Local MVP source checkout: pricing.yaml is version-controlled beside design.md.
DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "pricing.yaml"

#: `output_config.effort` — how much depth the planner's call is allowed. Spelled
#: out here rather than imported from the SDK so nothing below `orchestrator/`
#: has to know which vendor library the planner uses.
type PlannerEffort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AGENTHUB_",
        env_file=".env",
        extra="ignore",
    )

    # docs/architecture.md §4: agenthub.db, runs/ and workspaces/ live here.
    root: Path = Field(default=DEFAULT_ROOT)
    # Override to put the database somewhere other than <root>/agenthub.db.
    database_path: Path | None = Field(default=None)
    # SQLAlchemy statement logging. Off by default; it prints prompts.
    database_echo: bool = Field(default=False)
    # Kept configurable so replay and the live service cannot accidentally load
    # different histories when the checkout moves.
    pricing_path: Path = Field(default=DEFAULT_PRICING_PATH)
    # How many graph nodes may have a live agent at once (`design.md` §9).
    #
    # Two, not "as many as the graph allows". Each node is a whole CLI process
    # with its own model context, and they all draw on one account's rate limit:
    # ten in parallel exhausts the quota and leaves ten half-finished worktrees.
    # The ceiling is a guard against a typo in a `.env` turning into a fork
    # bomb, not a considered maximum — nothing here scales linearly past it.
    max_concurrency: int = Field(default=2, ge=1, le=16)

    # --- per-node cutoffs (`design.md` §9 and §12's runaway-agent risk) ------
    #
    # Denominated in **tokens**, summing all four fields of invariant 3, over
    # every `Usage` event of the run whatever its `source`. Not in estimated
    # cost, for two reasons. First, `PriceTable.cost_usd` returns `None` for a
    # model that is not in `pricing.yaml`, deliberately — so a cost-denominated
    # budget silently never fires for an unpriced model, which is exactly the
    # case a new release introduces and exactly when a loop goes unnoticed.
    # Second, invariant 7: under a Max/Pro subscription there is no per-token
    # billing at all, so a cost budget would be a limit on an estimate, while
    # the token count is a measured fact either way.
    #
    # Fifty million is a ceiling, not a target. Cache reads are ~90% of a long
    # session's four-field total (that is the ~100x of invariant 3), so a
    # genuinely long node lands in the millions to low tens of millions; a
    # looping agent passes this within the hour. Lower it per deployment — the
    # default exists so the mitigation is on out of the box, not because this
    # is the right number for every repository. `None` disables the cutoff.
    node_token_budget: Annotated[int, Field(ge=1)] | None = Field(default=50_000_000)
    # Wall clock for one *run*, measured from the moment the agent process is
    # launched. Worktree materialization happens before that and does not count
    # against it: a node whose base is a fold of five parents is doing git work,
    # not burning an agent. `None` disables the cutoff.
    node_timeout_s: Annotated[float, Field(gt=0)] | None = Field(default=3600.0)

    # --- planner (`design.md` §8) --------------------------------------------
    #
    # The planner is the one component that calls a model API directly instead
    # of driving a harness, so it is the one component whose model, depth and
    # ceiling are ours to choose rather than the CLI's.
    planner_model: str = Field(default="claude-opus-5")
    # Depth, not length. `high` because a graph is decided once and executed for
    # hours: the difference between a good decomposition and a mediocre one is
    # worth more than the tokens, and a bad graph costs a human a merge conflict
    # per wrong edge (`design.md` §12).
    planner_effort: PlannerEffort = Field(default="high")
    # Caps thinking *and* response text together, so this is not "how long may a
    # plan be" — a ten-node plan is a few thousand tokens and the rest is
    # headroom for adaptive thinking. The upper bound is the SDK's: a
    # non-streaming request whose max_tokens implies more than ten minutes of
    # generation raises rather than sending, and 21_333 is where that lands.
    planner_max_tokens: Annotated[int, Field(ge=2048, le=21_000)] = Field(
        default=16_000
    )
    # `design.md` §8's bounded correction loop, counted as total attempts: one
    # plan plus two corrections. A model that cannot close a two-node cycle in
    # three tries will not close it in thirty, and each try is real money.
    planner_max_attempts: Annotated[int, Field(ge=1, le=5)] = Field(default=3)
    # Which adapter a node gets when the planner suggests a harness that is not
    # installed. A configured *value*, not a conditional: nothing branches on
    # it (invariant 1), and `design.md` §8 makes the harness the operator's
    # choice in the editable proposal anyway — so an unusable suggestion is
    # worth a default, never a failed plan.
    planner_fallback_harness: str = Field(default="claude-code")
    # Where plans come from (`design.md` §8's backend seam).
    #
    # `harness` is the default because it is the one that works out of the box:
    # a Claude Max/Pro plan is not API access, so `api` on a fresh machine
    # means the Sessions tab answers 503 until the operator buys credit. The
    # tradeoff is honest and worth stating — `api` validates with
    # `messages.parse` against the Pydantic model in-process and reports
    # refusals and truncation through `stop_reason`, which a CLI cannot match.
    #
    # A value, never a conditional: nothing branches on which harness this
    # names (invariant 1). The adapter is asked whether it supports structured
    # output, and one that does not is a configuration error at startup.
    planner_backend: Literal["harness", "api"] = Field(default="harness")
    planner_harness: str = Field(default="claude-code")
    # None lets the CLI use whatever it is already configured for. Pinning it
    # here is for reproducibility, not capability.
    planner_harness_model: str | None = Field(default=None)

    # --- agentic code search (`design.md` §8, Phase 4 E4) -------------------
    # These are independent ceilings: raising one must never disable another.
    search_model: str = Field(default="claude-sonnet-5")
    search_max_output_tokens: Annotated[int, Field(ge=256, le=16_000)] = Field(
        default=4_096
    )
    search_max_turns: Annotated[int, Field(ge=1, le=32)] = Field(default=8)
    search_max_tool_calls: Annotated[int, Field(ge=1, le=100)] = Field(default=24)
    search_max_bytes: Annotated[int, Field(ge=1_024, le=8_388_608)] = Field(
        default=262_144
    )
    # All four token fields, including cache reads and writes (invariant 3).
    search_max_tokens: Annotated[int, Field(ge=1_024, le=10_000_000)] = Field(
        default=250_000
    )

    @property
    def db_path(self) -> Path:
        return self.database_path or self.root / "agenthub.db"

    @property
    def database_url(self) -> str:
        """The async URL. Everything on the event loop uses this one.

        ``sqlite3`` is synchronous and a blocking query stalls every PTY stream
        at once (invariant 5), so the application never opens a plain
        ``sqlite://`` connection. Alembic is the single exception and it runs
        off the loop — see ``app/storage/db.py:sync_url``.
        """
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def runs_root(self) -> Path:
        return self.root / "runs"

    @property
    def workspaces_root(self) -> Path:
        return self.root / "workspaces"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings. Cached: read once, not per request."""
    return Settings()
