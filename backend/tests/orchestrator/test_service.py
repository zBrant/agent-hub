"""The Phase 1 application service driven by a fake adapter and real git."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from app.config import Settings
from app.harnesses.base import ParseStats, RunHandle, RunSpec
from app.harnesses.events import (
    AgentEvent,
    PermissionDenial,
    RunFinished,
    RunStarted,
    RunStatus,
    TurnFinished,
    Usage,
)
from app.models.pricing import PriceTable, load_price_table
from app.models.status import NodeStatus, RunState, SessionStatus
from app.orchestrator.graph import RunBlockReason
from app.orchestrator.service import OrchestratorError, SingleRunService
from app.orchestrator.worktree import CommitStatus, MergeStatus
from app.storage.db import Database, upgrade_database_sync
from app.storage.meta import read_meta_sync
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
MODEL = "gpt-5.6-terra"


async def git(cwd: Path, *args: str) -> str:
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
    stdout, _ = await process.communicate()
    output = stdout.decode()
    assert process.returncode == 0, f"git {args} failed:\n{output}"
    return output


@pytest.fixture
async def target_repo(tmp_path: Path) -> Path:
    path = tmp_path / "target"
    path.mkdir()
    await git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("original\n", encoding="utf-8")
    await git(path, "add", "-A")
    await git(path, "commit", "-qm", "initial")
    return path


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(root=tmp_path / "agenthub", pricing_path=PRICING_YAML)


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    upgrade_database_sync(settings.database_url)
    database = Database.from_settings(settings)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def prices() -> PriceTable:
    return load_price_table(PRICING_YAML)


@dataclass
class FakeHandle:
    spec: RunSpec
    argv: tuple[str, ...]


class FakeAdapter:
    def __init__(
        self,
        *,
        name: str,
        status: str = "success",
        change: bool = True,
        permission_denials: int = 0,
        trusted: bool = True,
        release: asyncio.Event | None = None,
    ) -> None:
        self.name = name
        self.supported_models = [MODEL]
        self.status = status
        self.change = change
        self.permission_denials = permission_denials
        self.release = release
        self.stats = ParseStats(
            unknown={} if trusted else {"future-event": 1},
        )
        self.started: list[RunSpec] = []
        self.started_event = asyncio.Event()
        self.killed = False

    def build_argv(self, spec: RunSpec) -> list[str]:
        return [*spec.launcher, self.name, "--fake-json"]

    async def start(self, spec: RunSpec) -> RunHandle:
        self.started.append(spec)
        self.started_event.set()
        if self.change:
            (spec.cwd / "agent.txt").write_text("made by fake adapter\n")
        return cast(
            RunHandle,
            FakeHandle(spec=spec, argv=tuple(self.build_argv(spec))),
        )

    async def send(self, handle: RunHandle, text: str) -> None:
        raise NotImplementedError

    async def interrupt(self, handle: RunHandle) -> None:
        raise NotImplementedError

    async def kill(self, handle: RunHandle) -> None:
        self.killed = True

    async def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]:
        fake = cast(FakeHandle, handle)
        spec = fake.spec
        yield RunStarted(
            run_id=spec.run_id,
            ts=1_000,
            harness=self.name,
            model=spec.model,
            cwd=spec.cwd,
            pid=4242,
            session_id="fake-thread",
            harness_version="9.9.9",
        )
        if self.release is not None:
            await self.release.wait()
        yield Usage(
            run_id=spec.run_id,
            ts=1_010,
            model=spec.model or MODEL,
            input_tokens=10,
            output_tokens=20,
            cache_read_tokens=30,
            cache_write_tokens=40,
        )
        denials = tuple(
            PermissionDenial(tool=f"Write-{index}")
            for index in range(self.permission_denials)
        )
        yield TurnFinished(
            run_id=spec.run_id,
            ts=1_020,
            turn=1,
            status="success" if self.status == "success" else "failed",
            permission_denials=denials,
        )
        yield RunFinished(
            run_id=spec.run_id,
            ts=1_030,
            status=cast(RunStatus, self.status),
            exit_code=0 if self.status == "success" else 1,
        )


def service_for(
    *,
    database: Database,
    settings: Settings,
    prices: PriceTable,
    adapter: FakeAdapter,
    broadcasts: list[AgentEvent] | None = None,
) -> SingleRunService:
    async def broadcast(event: AgentEvent) -> None:
        if broadcasts is not None:
            broadcasts.append(event)

    def factory(name: str) -> FakeAdapter:
        assert name == adapter.name
        return adapter

    return SingleRunService(
        database=database,
        settings=settings,
        prices=prices,
        adapter_factory=factory,
        broadcast=broadcast,
        environment={
            "PATH": "/usr/bin",
            "HOME": "/Users/test",
            "ANTHROPIC_API_KEY": "must-never-reach-meta",
        },
    )


async def persisted(
    database: Database, session_id: str, node_id: str, run_id: str
) -> tuple[SessionStatus, NodeStatus, RunState, int]:
    async with database.session() as db_session:
        repository = Repository(db_session)
        session = await repository.get_session(session_id)
        node = await repository.get_node(node_id)
        run = await repository.get_run(run_id)
        assert session is not None and node is not None and run is not None
        return (
            session.status,
            node.status,
            run.status,
            run.permission_denial_count,
        )


async def test_fake_adapter_drives_the_complete_auto_merge_lifecycle(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    adapter = FakeAdapter(name="future-harness")
    broadcasts: list[AgentEvent] = []
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
        broadcasts=broadcasts,
    )
    created = await service.create_session(
        repo_path=target_repo,
        prompt="add agent.txt",
        harness=adapter.name,
        model=MODEL,
        auto_merge=True,
    )

    outcome = await service.run(created.session.id)

    assert outcome.run_status is RunState.SUCCESS
    assert outcome.node_status is NodeStatus.DONE
    assert outcome.trusted is True
    assert outcome.commit.status is CommitStatus.COMMITTED
    assert outcome.merge is not None
    assert outcome.merge.status is MergeStatus.MERGED
    assert outcome.totals.counts.total == 100
    assert [event.type for event in broadcasts] == [
        "run_started",
        "usage",
        "turn_finished",
        "run_finished",
    ]
    assert (created.session.workspace_root / "integration" / "agent.txt").exists()
    assert await persisted(
        database, created.session.id, created.node.id, outcome.run_id
    ) == (SessionStatus.DONE, NodeStatus.DONE, RunState.SUCCESS, 0)

    assert len(adapter.started) == 1
    spec = adapter.started[0]
    assert spec.cwd == created.node.worktree_path
    assert spec.launcher[:4] == (
        "ai-jail",
        "--clean",
        "--no-save-config",
        "--worktree",
    )
    assert "--mask" in spec.launcher
    assert "--deny-path" in spec.launcher

    meta = read_meta_sync(settings.runs_root / outcome.run_id / "meta.json")
    assert meta.harness == "future-harness"
    assert meta.harness_version == "9.9.9"
    assert meta.argv[-2:] == ("future-harness", "--fake-json")
    assert meta.env == {"HOME": "/Users/test", "PATH": "/usr/bin"}
    assert "must-never-reach-meta" not in (
        settings.runs_root / outcome.run_id / "meta.json"
    ).read_text(encoding="utf-8")


async def test_human_gate_waits_then_approve_merges(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    adapter = FakeAdapter(name="fake")
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
    )
    created = await service.create_session(
        repo_path=target_repo,
        prompt="add agent.txt",
        harness=adapter.name,
        model=MODEL,
    )

    outcome = await service.run(created.session.id)

    assert outcome.node_status is NodeStatus.AWAITING_REVIEW
    assert outcome.merge is None
    integration_file = created.session.workspace_root / "integration" / "agent.txt"
    assert not integration_file.exists()
    assert (await service.approve(created.session.id)).status is MergeStatus.MERGED
    assert integration_file.exists()
    assert await persisted(
        database, created.session.id, created.node.id, outcome.run_id
    ) == (SessionStatus.DONE, NodeStatus.DONE, RunState.SUCCESS, 0)


@pytest.mark.parametrize(
    ("trusted", "denials", "change", "status", "node_status", "reason"),
    [
        (
            False,
            0,
            True,
            "success",
            NodeStatus.BLOCKED,
            RunBlockReason.PARSER_UNTRUSTED,
        ),
        (
            True,
            1,
            True,
            "success",
            NodeStatus.BLOCKED,
            RunBlockReason.PERMISSION_DENIED,
        ),
        (
            True,
            0,
            False,
            "success",
            NodeStatus.BLOCKED,
            RunBlockReason.NO_CHANGES,
        ),
        (True, 0, True, "failed", NodeStatus.FAILED, None),
    ],
)
async def test_unsafe_or_failed_runs_never_merge(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
    trusted: bool,
    denials: int,
    change: bool,
    status: str,
    node_status: NodeStatus,
    reason: RunBlockReason | None,
) -> None:
    adapter = FakeAdapter(
        name="fake",
        trusted=trusted,
        permission_denials=denials,
        change=change,
        status=status,
    )
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
    )
    created = await service.create_session(
        repo_path=target_repo,
        prompt="try the work",
        harness=adapter.name,
        model=MODEL,
        auto_merge=True,
    )

    outcome = await service.run(created.session.id)

    assert outcome.node_status is node_status
    assert outcome.block_reason is reason
    assert outcome.merge is None
    assert not (created.session.workspace_root / "integration" / "agent.txt").exists()


async def test_a_second_run_is_refused_while_the_first_is_active(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    release = asyncio.Event()
    adapter = FakeAdapter(name="fake", release=release)
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
    )
    created = await service.create_session(
        repo_path=target_repo,
        prompt="add agent.txt",
        harness=adapter.name,
        model=MODEL,
    )
    first = asyncio.create_task(service.run(created.session.id))
    await adapter.started_event.wait()

    with pytest.raises(OrchestratorError, match="active run"):
        await service.run(created.session.id)

    release.set()
    await first


async def test_an_invalid_adapter_does_not_leave_a_running_attempt(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    def invalid_factory(name: str) -> FakeAdapter:
        raise ValueError(f"unknown harness {name}")

    service = SingleRunService(
        database=database,
        settings=settings,
        prices=prices,
        adapter_factory=invalid_factory,
    )
    with pytest.raises(ValueError, match="unknown harness"):
        await service.create_session(
            repo_path=target_repo,
            prompt="cannot start",
            harness="missing-harness",
            model=MODEL,
        )

    async with database.session() as db_session:
        repository = Repository(db_session)
        assert await repository.list_sessions() == []
