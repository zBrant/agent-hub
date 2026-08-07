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
