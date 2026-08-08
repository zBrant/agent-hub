"""Shared REST fixtures: a real database, real git worktrees, a fake harness.

`docs/architecture.md` §10 says API tests are smoke tests over the real service
— rules are proved in ``orchestrator/``. What is faked here is only the CLI, so
that a route test does not depend on a binary being installed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.harnesses.base import ParseStats, RunHandle, RunSpec
from app.harnesses.events import AgentEvent, RunFinished, RunStarted, RunStatus, Usage
from app.models.pricing import load_price_table
from app.orchestrator.service import NodeRunService

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


@dataclass
class FakeAdapter:
    """A harness that always succeeds and always changes one file.

    Each attempt writes different bytes, so a retry produces a real diff. An
    attempt whose worktree is byte-identical to the last one checkpoints
    nothing, and ``evaluate_run`` correctly refuses to gate it on a human — that
    is a real rule and not something a test should trip over by accident.
    """

    name: str = "fake"
    attempts: int = 0
    specs: list[RunSpec] = field(default_factory=list)
    supported_models: list[str] = field(default_factory=lambda: [MODEL])
    stats: ParseStats = field(default_factory=ParseStats)

    def build_argv(self, spec: RunSpec) -> list[str]:
        return [*spec.launcher, self.name, "--json"]

    async def start(self, spec: RunSpec) -> RunHandle:
        self.attempts += 1
        self.specs.append(spec)
        (spec.cwd / "api.txt").write_text(
            f"created through REST, attempt {self.attempts}\n", encoding="utf-8"
        )
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


def install_fake_service(client: TestClient, settings: Settings) -> FakeAdapter:
    """Swap in a service whose only difference is the harness it launches.

    ``broadcast`` and ``register_run`` are left at their defaults: the graph
    topic this activity adds is published by the *routes*, after a durable
    transition, so it must be observable without the service knowing a broker
    exists.
    """
    adapter = FakeAdapter()
    client.app.state.orchestrator = NodeRunService(
        database=client.app.state.database,
        settings=settings,
        prices=load_price_table(PRICING_YAML),
        adapter_factory=lambda name: adapter,
    )
    return adapter
