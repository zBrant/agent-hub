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
    ToolCall,
    TurnFinished,
    Usage,
)
from app.models.pricing import PriceTable, load_price_table
from app.models.status import NodeStatus, RunState, SessionStatus
from app.models.tables import Node
from app.orchestrator.graph import RunBlockReason, session_status_for_node
from app.orchestrator.service import (
    InvalidGraphError,
    InvalidTransitionError,
    NodeRunService,
    OrchestratorError,
    PlannedNode,
    session_status_for_nodes,
)
from app.orchestrator.worktree import CommitStatus, MergeStatus
from app.storage.db import Database, upgrade_database_sync
from app.storage.meta import read_meta_sync
from app.storage.ndjson import read_events
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
        commit_change: bool = False,
    ) -> None:
        self.name = name
        self.supported_models = [MODEL]
        self.status = status
        self.change = change
        self.permission_denials = permission_denials
        self.release = release
        self.commit_change = commit_change
        self.stats = ParseStats(
            unknown={} if trusted else {"future-event": 1},
        )
        self.started: list[RunSpec] = []
        self.started_event = asyncio.Event()
        self.tool_started = asyncio.Event()
        self.killed = False

    def build_argv(self, spec: RunSpec) -> list[str]:
        return [*spec.launcher, self.name, "--fake-json"]

    async def start(self, spec: RunSpec) -> RunHandle:
        self.started.append(spec)
        self.started_event.set()
        if self.change:
            # The prompt is in the payload so two nodes of one graph writing the
            # same path produce a real add/add conflict when their branches are
            # folded together, rather than two identical files git merges away.
            (spec.cwd / "agent.txt").write_text(
                f"made by fake adapter: {self.status}\n{spec.prompt}\n"
            )
            if self.commit_change:
                await git(spec.cwd, "add", "--all", "--")
                await git(spec.cwd, "commit", "-m", "feat: agent checkpoint")
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
        self.status = "interrupted"
        if self.release is not None:
            self.release.set()

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
            yield ToolCall(
                run_id=spec.run_id,
                ts=1_005,
                call_id="active-tool",
                tool="shell",
                input={"command": "long task"},
            )
            self.tool_started.set()
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
    registrations: list[tuple[str, str]] | None = None,
    transitions: list[NodeStatus] | None = None,
) -> NodeRunService:
    async def broadcast(event: AgentEvent) -> None:
        if broadcasts is not None:
            assert registrations
            broadcasts.append(event)

    async def register_run(run_id: str, session_id: str) -> None:
        if registrations is not None:
            registrations.append((run_id, session_id))

    async def on_transition(node: Node) -> None:
        if transitions is not None:
            transitions.append(node.status)

    def factory(name: str) -> FakeAdapter:
        assert name == adapter.name
        return adapter

    return NodeRunService(
        database=database,
        settings=settings,
        prices=prices,
        adapter_factory=factory,
        broadcast=broadcast,
        register_run=register_run,
        on_transition=on_transition,
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
    registrations: list[tuple[str, str]] = []
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
        broadcasts=broadcasts,
        registrations=registrations,
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
    assert registrations == [(outcome.run_id, created.session.id)]
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


async def test_agent_authored_commit_reaches_review_instead_of_blocking(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    adapter = FakeAdapter(name="fake", commit_change=True)
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
    )
    created = await service.create_session(
        repo_path=target_repo,
        prompt="commit the work yourself",
        harness=adapter.name,
        model=MODEL,
    )

    outcome = await service.run(created.session.id)

    assert outcome.run_status is RunState.SUCCESS
    assert outcome.node_status is NodeStatus.AWAITING_REVIEW
    assert outcome.block_reason is None
    assert outcome.commit.status is CommitStatus.CHECKPOINTED
    assert outcome.commit.changed_paths == (Path("agent.txt"),)


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


async def test_kill_during_a_tool_persists_interruption_and_never_merges(
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
        prompt="start a long tool",
        harness=adapter.name,
        model=MODEL,
        auto_merge=True,
    )
    running = asyncio.create_task(service.run(created.session.id))
    await adapter.tool_started.wait()

    killed = await service.kill(created.session.id)
    outcome = await running

    assert adapter.killed is True
    assert killed.id == outcome.run_id
    assert killed.status is RunState.INTERRUPTED
    assert outcome.run_status is RunState.INTERRUPTED
    assert outcome.node_status is NodeStatus.FAILED
    assert outcome.merge is None
    assert not (created.session.workspace_root / "integration" / "agent.txt").exists()
    events = list(read_events(settings.runs_root / outcome.run_id / "events.ndjson"))
    assert [event.type for event in events] == [
        "run_started",
        "tool_call",
        "usage",
        "turn_finished",
        "run_finished",
    ]
    assert isinstance(events[-1], RunFinished)
    assert events[-1].status == "interrupted"


async def test_retry_creates_a_new_run_and_preserves_failed_attempt(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    adapter = FakeAdapter(name="fake", status="failed")
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
    )
    created = await service.create_session(
        repo_path=target_repo,
        prompt="repair the implementation",
        harness=adapter.name,
        model=MODEL,
        auto_merge=True,
    )
    failed = await service.run(created.session.id)
    adapter.status = "success"

    retried = await service.retry(created.session.id)

    assert failed.run_status is RunState.FAILED
    assert retried.run_status is RunState.SUCCESS
    assert retried.node_status is NodeStatus.DONE
    assert retried.run_id != failed.run_id
    async with database.session() as db_session:
        runs = await Repository(db_session).list_runs(created.node.id)
    assert [(run.id, run.attempt, run.status) for run in runs] == [
        (failed.run_id, 1, RunState.FAILED),
        (retried.run_id, 2, RunState.SUCCESS),
    ]
    assert (settings.runs_root / failed.run_id / "events.ndjson").exists()
    assert (settings.runs_root / retried.run_id / "events.ndjson").exists()
    assert failed.commit.commit is not None
    assert created.node.worktree_path is not None
    assert (
        await git(
            created.node.worktree_path,
            "cat-file",
            "-t",
            failed.commit.commit,
        )
        == "commit\n"
    )


async def test_kill_and_retry_reject_invalid_states(
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
        prompt="ordinary run",
        harness=adapter.name,
        model=MODEL,
    )
    with pytest.raises(InvalidTransitionError, match="no active run"):
        await service.kill(created.session.id)
    with pytest.raises(InvalidTransitionError, match="only failed or blocked"):
        await service.retry(created.session.id)


@pytest.mark.parametrize("status", list(NodeStatus))
def test_the_graph_projection_agrees_with_phase_1_on_a_single_node(
    status: NodeStatus,
) -> None:
    """The generalization has to be a superset, or Phase 1's badge changes."""
    assert session_status_for_nodes([status]) is session_status_for_node(status)


def test_the_graph_projection_ranks_work_over_gates_over_waiting() -> None:
    assert session_status_for_nodes([]) is SessionStatus.PLANNING
    assert (
        session_status_for_nodes([NodeStatus.RUNNING, NodeStatus.BLOCKED])
        is SessionStatus.RUNNING
    )
    assert (
        session_status_for_nodes([NodeStatus.DONE, NodeStatus.AWAITING_REVIEW])
        is SessionStatus.PAUSED
    )
    # A failure with dependents still resolvable is a human's problem, not a
    # finished graph; a failure with nothing left to do is a failed graph.
    assert (
        session_status_for_nodes([NodeStatus.FAILED, NodeStatus.BLOCKED])
        is SessionStatus.PAUSED
    )
    assert (
        session_status_for_nodes([NodeStatus.DONE, NodeStatus.FAILED])
        is SessionStatus.FAILED
    )
    assert (
        session_status_for_nodes([NodeStatus.SKIPPED, NodeStatus.DONE])
        is SessionStatus.DONE
    )


async def test_a_conflicting_parent_fold_blocks_the_child_without_an_agent(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """`design.md` §2.2: the base is built before anything is launched.

    Both parents wrote the same file, so the join's worktree cannot exist as
    the merge of the two. That is the node's ``blocked`` state, reported as a
    value — and no harness process is ever created for it.
    """
    adapters: list[FakeAdapter] = []

    def factory(name: str) -> FakeAdapter:
        adapter = FakeAdapter(name=name)
        adapters.append(adapter)
        return adapter

    service = NodeRunService(
        database=database,
        settings=settings,
        prices=prices,
        adapter_factory=factory,
        environment={"PATH": "/usr/bin", "HOME": "/Users/test"},
    )
    graph = await service.create_graph(
        repo_path=target_repo,
        nodes=[
            PlannedNode(name="left", prompt="left", harness="fake", model=MODEL),
            PlannedNode(name="right", prompt="right", harness="fake", model=MODEL),
            PlannedNode(
                name="join",
                prompt="join",
                harness="fake",
                model=MODEL,
                depends_on=("left", "right"),
            ),
        ],
    )
    ids = graph.ids_by_name

    left = await service.start_node(ids["left"])
    right = await service.start_node(ids["right"])
    assert left.status is NodeStatus.AWAITING_REVIEW
    assert right.status is NodeStatus.AWAITING_REVIEW
    launched = len(adapters)

    join = await service.start_node(ids["join"], parents=(ids["left"], ids["right"]))

    assert join.status is NodeStatus.BLOCKED
    assert join.outcome is None
    assert join.conflicts == (Path("agent.txt"),)
    # Nothing was started for it: no adapter, no run row.
    assert len(adapters) == launched
    async with database.session() as db_session:
        repository = Repository(db_session)
        node = await repository.get_node(ids["join"])
        assert node is not None and node.status is NodeStatus.BLOCKED
        assert await repository.list_runs(ids["join"]) == []


async def test_an_invalid_adapter_does_not_leave_a_running_attempt(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    def invalid_factory(name: str) -> FakeAdapter:
        raise ValueError(f"unknown harness {name}")

    service = NodeRunService(
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


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing", "is not a git repository"),
        ("not-a-repo", "is not a git repository"),
        ("bad-base-ref", "has no commit for base ref"),
    ],
)
async def test_an_unusable_repo_path_is_rejected_as_an_argument(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
    tmp_path: Path,
    kind: str,
    expected: str,
) -> None:
    """A bad `repo_path` blames the request, not the server.

    Regression: `worktree.NotARepositoryError` is a `WorktreeError`, not a
    `ValueError`, so it escaped `api/deps.call`'s vocabulary and reached the
    client as an opaque 500 with no body. That is the *first* thing anyone hits
    — the README tells them to substitute their own path — and "Internal Server
    Error" gives them nothing to correct.
    """
    if kind == "missing":
        repo_path, base_ref = tmp_path / "nowhere", "HEAD"
    elif kind == "not-a-repo":
        plain = tmp_path / "plain"
        plain.mkdir()
        repo_path, base_ref = plain, "HEAD"
    else:
        repo_path, base_ref = target_repo, "no-such-branch"

    adapter = FakeAdapter(name="fake")
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
    )
    with pytest.raises(ValueError, match=expected):
        await service.create_session(
            repo_path=repo_path,
            prompt="does not matter",
            harness=adapter.name,
            model=MODEL,
            base_ref=base_ref,
        )

    async with database.session() as db_session:
        assert await Repository(db_session).list_sessions() == []


async def test_a_pending_proposal_can_be_edited_validated_and_approved(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    adapter = FakeAdapter(name="fake")
    transitions: list[NodeStatus] = []
    service = service_for(
        database=database,
        settings=settings,
        prices=prices,
        adapter=adapter,
        transitions=transitions,
    )
    created = await service.create_graph(
        repo_path=target_repo,
        nodes=(
            PlannedNode(name="a", prompt="do a", harness="fake", model=MODEL),
            PlannedNode(name="b", prompt="do b", harness="fake", model=MODEL),
        ),
    )
    a = created.ids_by_name["a"]
    b = created.ids_by_name["b"]

    updated = await service.update_node(
        b,
        name="renamed",
        prompt="do the renamed activity",
        harness="fake",
        model=MODEL,
        acceptance_criteria=("it works",),
        touches=("backend/**",),
        estimated_effort="small",
    )
    assert updated.name == "renamed"
    assert updated.acceptance_criteria == ("it works",)

    graph = await service.add_dependency(b, a)
    assert graph.depends_on()[b] == frozenset({a})
    assert (await service.get_graph(created.session.id)).edges == graph.edges

    with pytest.raises(InvalidGraphError, match="cycle") as cycle:
        await service.add_dependency(a, b)
    assert set(cycle.value.errors[0].nodes) == {a, b}
    assert len((await service.get_graph(created.session.id)).edges) == 1

    approved = await service.approve_graph(created.session.id)
    statuses = {node.id: node.status for node in approved.nodes}
    assert statuses == {a: NodeStatus.READY, b: NodeStatus.PENDING}
    assert transitions == [NodeStatus.READY]

    with pytest.raises(InvalidTransitionError, match="no longer an editable"):
        await service.update_node(
            b,
            name="too-late",
            prompt="too late",
            harness="fake",
            model=MODEL,
        )
    with pytest.raises(InvalidTransitionError, match="no longer an editable"):
        await service.remove_dependency(b, a)
