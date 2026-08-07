"""B5 REST contract over the real service, database, and git worktrees."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.harnesses.base import ParseStats, RunHandle, RunSpec
from app.harnesses.events import (
    AgentEvent,
    RunFinished,
    RunStarted,
    RunStatus,
    Usage,
)
from app.main import create_app
from app.models.pricing import load_price_table
from app.orchestrator.service import SingleRunService

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
MODEL = "gpt-5.6-terra"


async def git(cwd: Path, *args: str) -> None:
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
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    path = tmp_path / "target"
    path.mkdir()
    asyncio.run(git(path, "init", "-q", "-b", "main"))
    (path / "README.md").write_text("original\n", encoding="utf-8")
    asyncio.run(git(path, "add", "-A"))
    asyncio.run(git(path, "commit", "-qm", "initial"))
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(root=tmp_path / "agenthub", pricing_path=PRICING_YAML)


@dataclass
class FakeHandle:
    spec: RunSpec


class FakeAdapter:
    name = "fake"

    def __init__(self) -> None:
        self.supported_models = [MODEL]
        self.stats = ParseStats()

    def build_argv(self, spec: RunSpec) -> list[str]:
        return [*spec.launcher, self.name, "--json"]

    async def start(self, spec: RunSpec) -> RunHandle:
        (spec.cwd / "api.txt").write_text("created through REST\n", encoding="utf-8")
        return cast(RunHandle, FakeHandle(spec))

    async def send(self, handle: RunHandle, text: str) -> None:
        raise NotImplementedError

    async def interrupt(self, handle: RunHandle) -> None:
        raise NotImplementedError

    async def kill(self, handle: RunHandle) -> None:
        return None

    async def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]:
        spec = cast(FakeHandle, handle).spec
        yield RunStarted(
            run_id=spec.run_id,
            ts=1_000,
            harness=self.name,
            model=MODEL,
            cwd=spec.cwd,
            pid=123,
            harness_version="1.0.0",
        )
        yield Usage(
            run_id=spec.run_id,
            ts=1_010,
            model=MODEL,
            input_tokens=1,
            output_tokens=2,
            cache_read_tokens=3,
            cache_write_tokens=4,
        )
        yield RunFinished(
            run_id=spec.run_id,
            ts=1_020,
            status=cast(RunStatus, "success"),
            exit_code=0,
        )


def install_fake_service(client: TestClient, settings: Settings) -> None:
    adapter = FakeAdapter()
    client.app.state.orchestrator = SingleRunService(
        database=client.app.state.database,
        settings=settings,
        prices=load_price_table(PRICING_YAML),
        adapter_factory=lambda name: adapter,
    )


def test_complete_rest_flow_and_persisted_reconnect(
    settings: Settings, target_repo: Path
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/sessions",
            json={
                "repo_path": str(target_repo),
                "prompt": "create api.txt",
                "harness": "fake",
                "model": MODEL,
                "auto_merge": True,
            },
        )
        assert created.status_code == 201
        payload = created.json()
        session_id = payload["session"]["id"]
        node_id = payload["node"]["id"]
        assert payload["node"]["status"] == "ready"
        assert len(client.get("/api/sessions").json()) == 1
        assert client.get(f"/api/sessions/{session_id}").status_code == 200
        assert client.get(f"/api/sessions/{session_id}/node").json()["id"] == node_id

        started = client.post(f"/api/sessions/{session_id}/runs")
        assert started.status_code == 200
        result = started.json()
        assert result["run_status"] == "success"
        assert result["node_status"] == "done"
        assert result["trusted"] is True
        assert result["tokens"]["total_tokens"] == 10
        assert result["merged"] is True

        history = client.get(f"/api/sessions/{session_id}/runs").json()
        assert len(history) == 1
        assert history[0]["id"] == result["run_id"]
        assert history[0]["event_count"] == 3
        patch = client.get(f"/api/sessions/{session_id}/diff").json()["patch"]
        assert "api.txt" in patch
        assert "+created through REST" in patch

        # Done → run is an invalid transition, not a 500.
        refused = client.post(f"/api/sessions/{session_id}/runs")
        assert refused.status_code == 409

    # A fresh app/service reads the same authored and projected state. No
    # in-memory object from the first TestClient is involved.
    with TestClient(create_app(settings)) as reconnected:
        assert reconnected.get(f"/api/sessions/{session_id}").json()["status"] == "done"
        assert (
            reconnected.get(f"/api/sessions/{session_id}/node").json()["id"] == node_id
        )
        assert len(reconnected.get(f"/api/sessions/{session_id}/runs").json()) == 1
        assert (
            "api.txt"
            in reconnected.get(f"/api/sessions/{session_id}/diff").json()["patch"]
        )


def test_missing_resources_and_invalid_bodies_are_transport_errors(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/sessions/sess_missing").status_code == 404
        assert client.get("/api/sessions/sess_missing/node").status_code == 404
        assert client.get("/api/sessions/sess_missing/runs").status_code == 404
        assert client.post("/api/sessions/sess_missing/kill").status_code == 404
        assert client.post("/api/sessions/sess_missing/retry").status_code == 404
        invalid = client.post(
            "/api/sessions",
            json={"repo_path": "/tmp/repo", "prompt": "", "harness": "codex"},
        )
        assert invalid.status_code == 422
        unknown = client.post(
            "/api/sessions",
            json={
                "repo_path": str(target_repo),
                "prompt": "work",
                "harness": "missing-harness",
            },
        )
        assert unknown.status_code == 422
        assert client.get("/api/sessions").json() == []


def test_kill_and_retry_invalid_transitions_are_conflicts(
    settings: Settings, target_repo: Path
) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/sessions",
            json={
                "repo_path": str(target_repo),
                "prompt": "create api.txt",
                "harness": "fake",
                "model": MODEL,
            },
        ).json()
        session_id = created["session"]["id"]

        assert client.post(f"/api/sessions/{session_id}/kill").status_code == 409
        assert client.post(f"/api/sessions/{session_id}/retry").status_code == 409


def test_manual_approval_endpoint(settings: Settings, target_repo: Path) -> None:
    with TestClient(create_app(settings)) as client:
        install_fake_service(client, settings)
        created = client.post(
            "/api/sessions",
            json={
                "repo_path": str(target_repo),
                "prompt": "create api.txt",
                "harness": "fake",
                "model": MODEL,
            },
        ).json()
        session_id = created["session"]["id"]
        result = client.post(f"/api/sessions/{session_id}/runs").json()
        assert result["node_status"] == "awaiting_review"
        approved = client.post(f"/api/sessions/{session_id}/approve")
        assert approved.status_code == 200
        assert approved.json()["status"] == "merged"
