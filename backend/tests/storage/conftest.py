"""Fixtures for the SQLite projection.

Every database here is built by running the **real** Alembic migration against a
fresh file under ``tmp_path``. Two reasons, both cheap to get wrong:
``metadata.create_all`` would test a schema no user will ever have, and a
default ``~/.agenthub/agenthub.db`` in a fixture would let the test suite eat a
real session history.

``session_row`` / ``node_row`` / ``run_row`` are named for the tables rather
than for the domain so nothing in a test reads as the *SQLAlchemy* session.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.config import Settings
from app.models.ids import new_node_id, new_run_id, new_session_id
from app.models.pricing import PriceTable, load_price_table
from app.models.tables import Node, Run, Session
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
RUNS_ROOT = Path("/tmp/agenthub-test/runs")


def events_path(run_id: str) -> Path:
    """Where the source of truth for a run would live (`architecture.md` §4)."""
    return RUNS_ROOT / run_id / "events.ndjson"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings rooted in a throwaway directory, never ``~/.agenthub``."""
    return Settings(root=tmp_path / "agenthub-root")


@pytest.fixture
def migrated_url(settings: Settings) -> str:
    upgrade_database_sync(settings.database_url)
    return settings.database_url


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    db = Database(migrated_url)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def repo(database: Database) -> AsyncIterator[Repository]:
    async with database.session() as session:
        yield Repository(session)


@pytest.fixture
def prices() -> PriceTable:
    """The shipped price table, not a fake one: `design.md` §4 is the contract."""
    return load_price_table(PRICING_YAML)


@pytest.fixture
async def session_row(repo: Repository) -> Session:
    session_id = new_session_id()
    return await repo.create_session(
        session_id=session_id,
        title="add a docstring",
        repo_path=Path("/tmp/target-repo"),
        workspace_root=Path(f"/tmp/workspaces/{session_id}"),
        integration_branch=f"agenthub/{session_id}/integration",
    )


@pytest.fixture
async def node_row(repo: Repository, session_row: Session) -> Node:
    return await repo.create_node(
        node_id=new_node_id(),
        session_id=session_row.id,
        name="main",
        prompt="add a docstring to foo()",
        harness="codex",
        model="gpt-5.6-terra",
    )


@pytest.fixture
async def run_row(repo: Repository, node_row: Node) -> Run:
    run_id = new_run_id()
    return await repo.create_run(
        run_id=run_id, node_id=node_row.id, events_path=events_path(run_id)
    )
