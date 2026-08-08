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
from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ROOT = Path.home() / ".agenthub"
# Local MVP source checkout: pricing.yaml is version-controlled beside design.md.
DEFAULT_PRICING_PATH = Path(__file__).resolve().parents[2] / "pricing.yaml"


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
