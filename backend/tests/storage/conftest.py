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
from app.harnesses.events import (
    AgentEvent,
    AssistantText,
    PermissionDenial,
    RunFinished,
    RunStarted,
    ToolCall,
    ToolResult,
    TurnFinished,
    TurnStarted,
    Usage,
)
from app.models.ids import RunId, new_node_id, new_run_id, new_session_id
from app.models.pricing import PriceHistory, PriceTable, load_price_history
from app.models.tables import Node, Run, Session
from app.storage.db import Database, upgrade_database_sync
from app.storage.meta import RunMeta, build_meta
from app.storage.ndjson import events_path as run_log_path
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
RUNS_ROOT = Path("/tmp/agenthub-test/runs")

MODEL = "gpt-5.6-terra"


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
def price_history() -> PriceHistory:
    """The shipped price file, not a fake one: `design.md` §4 is the contract."""
    return load_price_history(PRICING_YAML)


@pytest.fixture
def prices(price_history: PriceHistory) -> PriceTable:
    return price_history.table()


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


@pytest.fixture
def runs_root(settings: Settings) -> Path:
    """``<root>/runs``, on a real filesystem this time."""
    return settings.runs_root


@pytest.fixture
async def logged_run(repo: Repository, node_row: Node, runs_root: Path) -> Run:
    """A run row whose ``events_path`` is a file we are actually going to write."""
    run_id = new_run_id()
    return await repo.create_run(
        run_id=run_id,
        node_id=node_row.id,
        events_path=run_log_path(runs_root, run_id),
        harness="codex",
        model=MODEL,
    )


@pytest.fixture
def run_meta(logged_run: Run, prices: PriceTable) -> RunMeta:
    """What B4 will build: the row first, then the metadata describing it.

    ``created_ms`` comes off the row rather than from a fresh clock read —
    replay recreates the row with this number, and a rebuilt row that disagrees
    about when it was created is not the same row.
    """
    return build_meta(
        run_id=logged_run.id,
        session_id=logged_run.session_id,
        node_id=logged_run.node_id,
        attempt=logged_run.attempt,
        price_table_version=prices.version,
        harness=logged_run.harness,
        harness_version="0.101.0",
        model=logged_run.model,
        cwd=Path("/tmp/workspaces/node_a"),
        argv=("ai-jail", "--clean", "codex", "exec", "--json"),
        env={"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-must-not-be-written"},
        created_ms=logged_run.created_ms,
    )


@pytest.fixture
def model() -> str:
    return MODEL


@pytest.fixture
def event_stream(logged_run: Run) -> list[AgentEvent]:
    """The whole log of :func:`sample_stream`, for the run under test."""
    return sample_stream(logged_run.id)


def sample_stream(run_id: RunId, *, model: str = MODEL) -> list[AgentEvent]:
    """A complete, ordinary run: two turns, two usage events, one refusal."""
    return [
        RunStarted(
            run_id=run_id,
            ts=1_000,
            harness="codex",
            model=model,
            cwd=Path("/tmp/workspaces/node_a"),
            pid=4242,
            session_id="thread-abc",
            harness_version="0.101.0",
        ),
        TurnStarted(run_id=run_id, ts=1_010, turn=1, model=model),
        AssistantText(run_id=run_id, ts=1_020, text="looking at foo()"),
        ToolCall(
            run_id=run_id,
            ts=1_030,
            call_id="call_1",
            tool="apply_patch",
            input={"path": "foo.py"},
        ),
        ToolResult(run_id=run_id, ts=1_040, call_id="call_1", ok=True, preview="ok"),
        Usage(
            run_id=run_id,
            ts=1_050,
            model=model,
            input_tokens=21,
            output_tokens=254,
            cache_read_tokens=21_737,
            cache_write_tokens=6_513,
            cache_write_1h_tokens=6_513,
        ),
        TurnFinished(run_id=run_id, ts=1_060, turn=1, status="success"),
        TurnStarted(run_id=run_id, ts=1_070, turn=2, model=model),
        Usage(
            run_id=run_id,
            ts=1_080,
            model=model,
            source="reconstructed",
            input_tokens=13,
            output_tokens=99,
            cache_read_tokens=1_000,
        ),
        TurnFinished(
            run_id=run_id,
            ts=1_090,
            turn=2,
            status="success",
            permission_denials=(PermissionDenial(tool="Write", call_id="call_2"),),
        ),
        RunFinished(
            run_id=run_id, ts=1_100, status="success", exit_code=0, summary="done"
        ),
    ]
