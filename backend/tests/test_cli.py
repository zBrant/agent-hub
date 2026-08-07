"""The installed command: local-only ``serve``, and ``replay`` end to end.

``agenthub replay`` is the executable form of invariant 4 — without it, "NDJSON
is the source of truth and SQLite is derived" is an unverifiable claim. These
tests drive the real command over a real migrated database and a real log.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml

from app import cli
from app.config import Settings
from app.harnesses.events import (
    AgentEvent,
    RunFinished,
    RunStarted,
    TurnFinished,
    TurnStarted,
    Usage,
)
from app.models.ids import new_node_id, new_run_id, new_session_id
from app.models.pricing import load_price_table
from app.storage.db import Database, upgrade_database_sync
from app.storage.ingest import ingest_run
from app.storage.meta import build_meta
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[2]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
MODEL = "gpt-5.6-terra"


def test_serve_binds_only_to_loopback(monkeypatch: Any) -> None:
    called: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        called.update(app=app, **kwargs)

    monkeypatch.setattr(cli.uvicorn, "run", fake_run)
    assert cli.main(["serve", "--port", "8123"]) == 0
    assert called == {
        "app": "app.main:app",
        "host": "127.0.0.1",
        "port": 8123,
    }


def stream(run_id: str) -> list[AgentEvent]:
    return [
        RunStarted(
            run_id=run_id,
            ts=1_000,
            harness="codex",
            model=MODEL,
            cwd=Path("/tmp/wt"),
            pid=1,
        ),
        TurnStarted(run_id=run_id, ts=1_010, turn=1, model=MODEL),
        Usage(
            run_id=run_id,
            ts=1_020,
            model=MODEL,
            input_tokens=1_000_000,
            output_tokens=1_000_000,
        ),
        TurnFinished(run_id=run_id, ts=1_030, turn=1, status="success"),
        RunFinished(run_id=run_id, ts=1_040, status="success", exit_code=0),
    ]


async def seed(settings: Settings) -> str:
    """A session, a node and one fully ingested run. Returns the run id."""
    await asyncio.to_thread(upgrade_database_sync, settings.database_url)
    prices = load_price_table(PRICING_YAML)

    database = Database.from_settings(settings)
    try:
        async with database.session() as session:
            repo = Repository(session)
            session_id = new_session_id()
            await repo.create_session(
                session_id=session_id,
                title="cli replay",
                repo_path=Path("/tmp/target-repo"),
                workspace_root=Path(f"/tmp/workspaces/{session_id}"),
                integration_branch=f"agenthub/{session_id}/integration",
            )
            node_id = new_node_id()
            await repo.create_node(
                node_id=node_id,
                session_id=session_id,
                name="main",
                prompt="add a docstring",
                harness="codex",
                model=MODEL,
            )
            run_id = new_run_id()
            run = await repo.create_run(
                run_id=run_id,
                node_id=node_id,
                events_path=settings.runs_root / run_id / "events.ndjson",
            )
            meta = build_meta(
                run_id=run.id,
                session_id=run.session_id,
                node_id=run.node_id,
                attempt=run.attempt,
                price_table_version=prices.version,
                harness=run.harness,
                model=run.model,
                cwd=Path("/tmp/wt"),
                created_ms=run.created_ms,
            )
            async with ingest_run(
                repository=repo,
                runs_root=settings.runs_root,
                meta=meta,
                prices=prices,
            ) as ingest:
                for event in stream(run_id):
                    await ingest.ingest(event)
            return run_id
    finally:
        await database.dispose()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "agenthub-root"


@pytest.fixture
def seeded_run(root: Path) -> str:
    return asyncio.run(seed(Settings(root=root)))


def test_replay_rebuilds_a_run_from_its_log(
    root: Path, seeded_run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["replay", seeded_run, "--root", str(root)])
    out = capsys.readouterr().out

    assert code == 0
    assert seeded_run in out
    assert "events        5" in out
    assert "usage rows    1" in out
    assert "status        success" in out
    # 1M in at $2.50 + 1M out at $15.00 for gpt-5.6-terra.
    assert "$17.5000 estimated equivalent" in out


def test_replay_is_idempotent_from_the_command_line(
    root: Path, seeded_run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["replay", seeded_run, "--root", str(root)]) == 0
    first = capsys.readouterr().out
    assert cli.main(["replay", seeded_run, "--root", str(root)]) == 0
    assert capsys.readouterr().out == first


def test_replay_refuses_a_run_whose_price_table_is_gone(
    root: Path, seeded_run: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The pinned version must be found or the command stops. No fallback."""
    future = yaml.safe_load(PRICING_YAML.read_text(encoding="utf-8"))
    future["version"] = 99
    future["superseded"] = []
    pricing = tmp_path / "pricing.yaml"
    pricing.write_text(yaml.safe_dump(future), encoding="utf-8")

    code = cli.main(
        ["replay", seeded_run, "--root", str(root), "--pricing", str(pricing)]
    )

    assert code == 1
    err = capsys.readouterr().err
    assert "price table version 1" in err
    assert "known versions: 99" in err


def test_replay_reports_an_unknown_run_without_a_traceback(
    root: Path, seeded_run: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["replay", "run_does_not_exist", "--root", str(root)])
    assert code == 1
    assert "replay refused" in capsys.readouterr().err


def test_replay_without_a_database_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli.main(["replay", "run_x", "--root", str(tmp_path / "empty")])
    assert code == 1
    assert "no database" in capsys.readouterr().err
