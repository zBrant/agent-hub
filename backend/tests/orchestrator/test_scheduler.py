"""The concurrent graph scheduler, driven by a fake adapter and real git.

Concurrency is sampled **from inside the adapter**, while an agent is streaming.
Counting tasks from the outside proves the scheduler's bookkeeping agrees with
itself; counting live agents proves the thing that actually matters, which is
how many CLI processes and how much rate limit are in use at one instant.

The probe also runs the tests in the other direction. A scheduler that is
accidentally sequential passes every assertion about final state, so the nodes
that are supposed to overlap block in a rendezvous until they all arrive: if
they never do, the test times out rather than quietly passing.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from structlog.testing import capture_logs

from app.config import Settings
from app.harnesses.base import ParseStats, RunHandle, RunSpec
from app.harnesses.events import (
    AgentEvent,
    RunFinished,
    RunStarted,
    TurnFinished,
    Usage,
)
from app.models.pricing import PriceTable, load_price_table
from app.models.status import NodeStatus, RunState, SessionStatus
from app.orchestrator.graph import DagErrorKind, GraphOutcome
from app.orchestrator.scheduler import GraphScheduler
from app.orchestrator.service import (
    CreatedGraph,
    InvalidGraphError,
    InvalidTransitionError,
    NodeExecution,
    NodeRunService,
    PlannedNode,
    ResourceNotFoundError,
)
from app.storage.db import Database, upgrade_database_sync
from app.storage.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[3]
PRICING_YAML = REPO_ROOT / "pricing.yaml"
MODEL = "gpt-5.6-terra"
HARNESS = "fake"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# A fake harness that reports what it is doing while it does it
# ---------------------------------------------------------------------------


class ConcurrencyProbe:
    """Live agent census, sampled from inside the adapter's event stream.

    ``rendezvous`` names the nodes that must be streaming *at the same time*.
    Each of them parks until every member has arrived, so a sequential
    scheduler deadlocks the test instead of passing it. Nodes outside the
    rendezvous only yield to the loop a few times, which is enough for the
    interleaving assertions without making any test depend on a sleep.
    """

    def __init__(self, *, rendezvous: Collection[str] = (), hold: bool = False) -> None:
        self.rendezvous = frozenset(rendezvous)
        # ``hold`` parks *every* arrival until the test releases it. The bound
        # test needs that: a size-based quorum releases the first pair before
        # the rest have materialized — worktree registration is serialized
        # (C4/C5) — so the census settles at the bound whether or not the bound
        # exists. Holding everyone makes the census the whole launched set.
        self.hold = hold
        self._released = asyncio.Event()
        self._census_target: int | None = None
        self._census_reached = asyncio.Event()
        self.active: set[str] = set()
        self.peak = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self._quorum = asyncio.Event()

    async def enter(self, node: str) -> None:
        self.active.add(node)
        self.started.append(node)
        self.peak = max(self.peak, len(self.active))
        if self.rendezvous and self.rendezvous <= self.active:
            self._quorum.set()
        if self._census_target is not None and len(self.active) >= self._census_target:
            self._census_reached.set()
        if self.hold:
            await self._released.wait()
        if node in self.rendezvous:
            async with asyncio.timeout(10):
                await self._quorum.wait()
        else:
            for _ in range(4):
                await asyncio.sleep(0)

    # No `timeout` parameter: ruff's ASYNC109 wants the caller to wrap the call
    # in `asyncio.timeout` instead, and it is right — a deadline belongs to the
    # caller's scope, not to the helper's signature.
    async def wait_for_census(self, count: int) -> None:
        """Block until ``count`` agents are streaming at once."""
        self._census_target = count
        if len(self.active) >= count:
            return
        await self._census_reached.wait()

    def release(self) -> None:
        """Let every parked agent finish. Called once the census has settled."""
        self._released.set()

    def leave(self, node: str) -> None:
        self.active.discard(node)
        self.finished.append(node)


@dataclass
class FakeHandle:
    spec: RunSpec
    node: str


@dataclass
class FakeHarness:
    """Adapter factory plus the per-node script the adapters follow.

    A fresh adapter per call, like ``app.harnesses.create_adapter``: adapters
    carry :class:`ParseStats`, and sharing one instance across concurrent runs
    would pool the parser's verdict on four different streams.
    """

    probe: ConcurrencyProbe = field(default_factory=ConcurrencyProbe)
    failing: frozenset[str] = frozenset()
    crashing: frozenset[str] = frozenset()
    on_start: Callable[[RunSpec], object] | None = None
    name: str = HARNESS
    adapters: list[FakeAdapter] = field(default_factory=list)

    def __call__(self, harness: str) -> FakeAdapter:
        assert harness == self.name
        adapter = FakeAdapter(self)
        self.adapters.append(adapter)
        return adapter


class FakeAdapter:
    def __init__(self, harness: FakeHarness) -> None:
        self.name = harness.name
        self.supported_models = [MODEL]
        self.stats = ParseStats()
        self.killed = False
        self._harness = harness

    def build_argv(self, spec: RunSpec) -> list[str]:
        return [*spec.launcher, self.name, "--fake-json"]

    async def start(self, spec: RunSpec) -> RunHandle:
        node = spec.prompt
        if node in self._harness.crashing:
            raise RuntimeError(f"launcher exploded for {node}")
        if self._harness.on_start is not None:
            result = self._harness.on_start(spec)
            if asyncio.iscoroutine(result):
                await result
        # One file per node, so two parallel nodes do not conflict by accident;
        # a test that wants a conflict writes the same name on purpose.
        (spec.cwd / f"{node}.txt").write_text(f"{node}\n", encoding="utf-8")
        return cast(RunHandle, FakeHandle(spec=spec, node=node))

    async def send(self, handle: RunHandle, text: str) -> None:
        raise NotImplementedError

    async def interrupt(self, handle: RunHandle) -> None:
        raise NotImplementedError

    async def kill(self, handle: RunHandle) -> None:
        self.killed = True

    async def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]:
        fake = cast(FakeHandle, handle)
        spec = fake.spec
        node = fake.node
        failed = node in self._harness.failing
        yield RunStarted(
            run_id=spec.run_id,
            ts=1_000,
            harness=self.name,
            model=spec.model,
            cwd=spec.cwd,
            pid=4242,
            session_id=f"fake-thread-{node}",
            harness_version="9.9.9",
        )
        await self._harness.probe.enter(node)
        try:
            yield Usage(
                run_id=spec.run_id,
                ts=1_010,
                model=spec.model or MODEL,
                input_tokens=10,
                output_tokens=20,
                cache_read_tokens=30,
                cache_write_tokens=40,
            )
            yield TurnFinished(
                run_id=spec.run_id,
                ts=1_020,
                turn=1,
                status="failed" if failed else "success",
            )
            yield RunFinished(
                run_id=spec.run_id,
                ts=1_030,
                status="failed" if failed else "success",
                exit_code=1 if failed else 0,
            )
        finally:
            self._harness.probe.leave(node)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_service(
    *,
    database: Database,
    settings: Settings,
    prices: PriceTable,
    harness: FakeHarness,
    broadcast: Callable[[AgentEvent], object] | None = None,
) -> NodeRunService:
    async def publish(event: AgentEvent) -> None:
        if broadcast is not None:
            result = broadcast(event)
            if asyncio.iscoroutine(result):
                await result

    return NodeRunService(
        database=database,
        settings=settings,
        prices=prices,
        adapter_factory=harness,
        broadcast=publish,
        environment={"PATH": "/usr/bin", "HOME": "/Users/test"},
    )


def plan(*specs: str) -> tuple[PlannedNode, ...]:
    """``"d:b,c"`` is a node named ``d`` that depends on ``b`` and ``c``.

    The prompt is the node's name, which is what the fake adapter keys its
    script on and what the file it writes is called.
    """
    planned: list[PlannedNode] = []
    for spec in specs:
        name, _, deps = spec.partition(":")
        planned.append(
            PlannedNode(
                name=name,
                prompt=name,
                harness=HARNESS,
                model=MODEL,
                depends_on=tuple(d for d in deps.split(",") if d),
            )
        )
    return tuple(planned)


async def statuses(database: Database, graph: CreatedGraph) -> dict[str, NodeStatus]:
    async with database.session() as db_session:
        rows = await Repository(db_session).list_nodes(graph.session.id)
    by_id = {row.id: row for row in rows}
    return {name: by_id[node_id].status for name, node_id in graph.ids_by_name.items()}


async def run_counts(database: Database, graph: CreatedGraph) -> dict[str, int]:
    async with database.session() as db_session:
        runs = await Repository(db_session).list_session_runs(graph.session.id)
    counts = dict.fromkeys(graph.ids_by_name, 0)
    by_id = {node_id: name for name, node_id in graph.ids_by_name.items()}
    for run in runs:
        counts[by_id[run.node_id]] += 1
    return counts


async def worktree_of(database: Database, node_id: str) -> Path:
    async with database.session() as db_session:
        node = await Repository(db_session).get_node(node_id)
    assert node is not None and node.worktree_path is not None
    return node.worktree_path


def scheduler_for(
    service: NodeRunService, database: Database, settings: Settings
) -> GraphScheduler:
    return GraphScheduler(lifecycle=service, database=database, settings=settings)


def log_lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


# ---------------------------------------------------------------------------
# The bound
# ---------------------------------------------------------------------------


def test_max_concurrency_defaults_to_two(tmp_path: Path) -> None:
    """`design.md` §9: low by default. Each node is a whole CLI process."""
    assert Settings(root=tmp_path).max_concurrency == 2


async def test_a_diamond_completes_without_ever_exceeding_max_concurrency(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    harness = FakeHarness(probe=ConcurrencyProbe(rendezvous={"b", "c"}))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo,
        nodes=plan("a", "b:a", "c:a", "d:b,c"),
        auto_merge=True,
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.COMPLETE
    assert result.succeeded
    assert await statuses(database, graph) == dict.fromkeys("abcd", NodeStatus.DONE)
    # Sampled while agents were live, never after the fact.
    assert harness.probe.peak == 2
    assert harness.probe.started[0] == "a"
    assert harness.probe.started[-1] == "d"
    assert set(harness.probe.started[1:3]) == {"b", "c"}
    assert await run_counts(database, graph) == dict.fromkeys("abcd", 1)

    # C4's done-when, driven from the graph: the join's base is the merge of
    # both parents, so its worktree carries both branches' edits.
    joined = await worktree_of(database, graph.ids_by_name["d"])
    assert (joined / "a.txt").exists()
    assert (joined / "b.txt").exists()
    assert (joined / "c.txt").exists()

    integration = graph.session.workspace_root / "integration"
    assert sorted(p.name for p in integration.glob("*.txt")) == [
        "a.txt",
        "b.txt",
        "c.txt",
        "d.txt",
    ]
    async with database.session() as db_session:
        session = await Repository(db_session).get_session(graph.session.id)
    assert session is not None and session.status is SessionStatus.DONE


async def test_two_independent_nodes_really_overlap_in_time(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """A sequential scheduler passes every other test in this file.

    Both nodes park in the rendezvous until the other arrives, so this test
    can only complete if they are streaming at the same instant.
    """
    harness = FakeHarness(probe=ConcurrencyProbe(rendezvous={"x", "y"}))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("x", "y"), auto_merge=True
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.COMPLETE
    assert harness.probe.peak == 2
    assert await statuses(database, graph) == {
        "x": NodeStatus.DONE,
        "y": NodeStatus.DONE,
    }


async def test_the_bound_is_what_holds_the_peak_down(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """More startable nodes than slots — the only test the bound can fail.

    The other concurrency assertions here cannot fail: a diamond offers at most
    two parallel nodes, so a scheduler with no bound at all still peaks at two,
    and a node outside the rendezvous only yields a few times, which is far
    shorter than the git work a sibling is doing. Removing the bound leaves
    every one of them green.

    So this one gives four independent nodes two slots, and parks each arriving
    agent until as many as the bound allows are streaming together. With the
    bound the census settles at exactly two; without it all four are launched
    before any can finish, the quorum releases them, and the peak is four.
    """
    limit = 2
    bounded = settings.model_copy(update={"max_concurrency": limit})
    names = ("w", "x", "y", "z")
    probe = ConcurrencyProbe(hold=True)
    harness = FakeHarness(probe=probe)
    service = build_service(
        database=database, settings=bounded, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan(*names), auto_merge=True
    )

    running = asyncio.create_task(
        scheduler_for(service, database, bounded).run_graph(graph.session.id)
    )
    try:
        # Wait for the slots to fill, then keep waiting. The first half makes
        # the sample deterministic — never taken before the bound is reached,
        # so a slow machine cannot pass this by reading a 1. The second is the
        # window an unbounded scheduler needs to get its third and fourth
        # agents streaming: worktree registration is serialized (C4/C5), so
        # they arrive after the first two rather than with them.
        async with asyncio.timeout(20):
            await probe.wait_for_census(limit)
        await asyncio.sleep(0.5)
        observed = probe.peak
    finally:
        probe.release()

    result = await running

    assert result.outcome is GraphOutcome.COMPLETE
    assert sorted(probe.finished) == sorted(names)
    # The assertion with teeth: unbounded, this is len(names).
    assert observed == limit


async def test_max_concurrency_one_never_runs_two_agents_at_once(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    harness = FakeHarness()
    serial = settings.model_copy(update={"max_concurrency": 1})
    service = build_service(
        database=database, settings=serial, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("x", "y", "z"), auto_merge=True
    )

    result = await scheduler_for(service, database, serial).run_graph(graph.session.id)

    assert result.outcome is GraphOutcome.COMPLETE
    assert harness.probe.peak == 1
    assert harness.probe.started == harness.probe.finished


# ---------------------------------------------------------------------------
# Failure is data
# ---------------------------------------------------------------------------


async def test_a_failure_blocks_its_transitive_dependents_by_name(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """`docs/phase-2.md` C3: ``blocked``, not ``pending`` forever.

    ``c`` is two edges below the failure, and the cause it is blocked by is
    ``a`` — not ``b``, which never even started.
    """
    harness = FakeHarness(failing=frozenset({"a"}))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo,
        nodes=plan("a", "b:a", "c:b", "d"),
        auto_merge=True,
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    ids = graph.ids_by_name
    assert result.outcome is GraphOutcome.WAITING_ON_HUMAN
    assert await statuses(database, graph) == {
        "a": NodeStatus.FAILED,
        "b": NodeStatus.BLOCKED,
        "c": NodeStatus.BLOCKED,
        "d": NodeStatus.DONE,
    }
    assert result.blocked_by(ids["b"]) == (ids["a"],)
    assert result.blocked_by(ids["c"]) == (ids["a"],)
    # No agent was launched for a node that could never succeed.
    assert await run_counts(database, graph) == {"a": 1, "b": 0, "c": 0, "d": 1}
    assert "d" in harness.probe.started and "b" not in harness.probe.started

    # ...and the unrelated branch really landed.
    integration = graph.session.workspace_root / "integration"
    assert (integration / "d.txt").exists()
    assert not (integration / "a.txt").exists()


async def test_a_crashing_launch_fails_its_node_and_spares_the_rest(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """An exception is a bug, but it must not take the scheduler down with it."""
    harness = FakeHarness(crashing=frozenset({"a"}))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b"), auto_merge=True
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.COMPLETE
    assert not result.succeeded
    assert await statuses(database, graph) == {
        "a": NodeStatus.FAILED,
        "b": NodeStatus.DONE,
    }
    async with database.session() as db_session:
        runs = await Repository(db_session).list_session_runs(graph.session.id)
    crashed = [run for run in runs if run.node_id == graph.ids_by_name["a"]]
    assert [run.status for run in crashed] == [RunState.INTERRUPTED]


async def test_a_deadlock_is_reported_loudly_rather_than_silently_exited(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """C2 narrowed ``deadlocked`` to "a transition was not persisted".

    So the way to reach it is to not persist one. Suppressing the blocked
    propagation leaves the dependents of a failed node ``pending``, which is
    exactly the bug the outcome exists to detect.
    """

    async def no_propagation(
        self: GraphScheduler, evaluation: object, blocked_causes: object
    ) -> tuple[object, ...]:
        return ()

    monkeypatch.setattr(GraphScheduler, "_propagate_blocked", no_propagation)

    harness = FakeHarness(failing=frozenset({"a"}))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=True
    )

    with capture_logs() as entries:
        result = await scheduler_for(service, database, settings).run_graph(
            graph.session.id
        )

    assert result.outcome is GraphOutcome.DEADLOCKED
    assert await statuses(database, graph) == {
        "a": NodeStatus.FAILED,
        "b": NodeStatus.PENDING,
    }
    reported = [entry for entry in entries if entry["event"] == "scheduler.deadlocked"]
    assert len(reported) == 1
    assert reported[0]["log_level"] == "error"
    assert reported[0]["pending"] == [graph.ids_by_name["b"]]


async def test_a_running_row_this_process_does_not_own_is_reported_not_spun_on(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """A ``running`` row left by a dead orchestrator is C6's to resolve.

    Until then the graph is ``active`` with nothing startable and nothing of
    ours in flight, which is the one shape that would loop forever. The test
    hangs rather than fails if that regresses.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=True
    )
    async with database.session() as db_session:
        await Repository(db_session).set_node_status(
            graph.ids_by_name["a"], NodeStatus.RUNNING
        )

    with capture_logs() as entries:
        result = await asyncio.wait_for(
            scheduler_for(service, database, settings).run_graph(graph.session.id),
            timeout=10,
        )

    assert result.outcome is GraphOutcome.ACTIVE
    assert result.unowned_running == (graph.ids_by_name["a"],)
    assert harness.probe.started == []
    reported = [
        entry
        for entry in entries
        if entry["event"] == "scheduler.unowned_running_nodes"
    ]
    assert len(reported) == 1
    assert reported[0]["log_level"] == "error"


async def test_a_node_owned_by_someone_else_is_stood_down_from_not_failed(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """A refusal is not a crash.

    ``start_node`` refuses when another caller holds the node's slot or it
    already has a live run row — which C9's per-node run route makes reachable.
    Forcing the node to ``failed`` there would overwrite the state of a run
    that is working. The loop must stand down and still terminate.
    """

    class RefusingLifecycle:
        def __init__(self) -> None:
            self.attempts = 0

        async def start_node(
            self, node_id: str, *, parents: Sequence[str] = ()
        ) -> NodeExecution:
            self.attempts += 1
            raise InvalidTransitionError(f"node {node_id} already has an active run")

        async def block_node(self, node_id: str, *, causes: Sequence[str]) -> bool:
            return False

        async def fail_node(self, node_id: str, *, reason: str) -> bool:
            raise AssertionError("a refused node must never be marked failed")

    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=FakeHarness(),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )
    lifecycle = RefusingLifecycle()

    result = await asyncio.wait_for(
        GraphScheduler(
            lifecycle=lifecycle, database=database, settings=settings
        ).run_graph(graph.session.id),
        timeout=10,
    )

    assert lifecycle.attempts == 1
    assert result.outcome is GraphOutcome.ACTIVE
    assert (await statuses(database, graph))["a"] is NodeStatus.PENDING


async def test_an_unknown_session_is_a_not_found_error(
    database: Database, settings: Settings, prices: PriceTable
) -> None:
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=FakeHarness(),
    )
    with pytest.raises(ResourceNotFoundError):
        await scheduler_for(service, database, settings).run_graph("sess_missing")


# ---------------------------------------------------------------------------
# The human gate
# ---------------------------------------------------------------------------


async def test_auto_merge_off_stops_at_awaiting_review_and_holds_dependents(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Invariant 6. ``awaiting_review`` does not satisfy a dependent."""
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=False
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.WAITING_ON_HUMAN
    assert await statuses(database, graph) == {
        "a": NodeStatus.AWAITING_REVIEW,
        "b": NodeStatus.PENDING,
    }
    assert await run_counts(database, graph) == {"a": 1, "b": 0}
    assert harness.probe.started == ["a"]
    assert not (graph.session.workspace_root / "integration" / "a.txt").exists()
    async with database.session() as db_session:
        session = await Repository(db_session).get_session(graph.session.id)
    assert session is not None and session.status is SessionStatus.PAUSED

    # The gate opens and the graph continues from where it stopped.
    await service.approve_node(graph.ids_by_name["a"])
    resumed = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )
    assert resumed.outcome is GraphOutcome.WAITING_ON_HUMAN
    assert (await statuses(database, graph))["b"] is NodeStatus.AWAITING_REVIEW


# ---------------------------------------------------------------------------
# Persistence and the write path
# ---------------------------------------------------------------------------


async def test_the_node_is_running_in_the_database_before_its_agent_starts(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """ "Persist on every transition, before acting on it."

    The node worktree's directory name is its id, so the adapter can look
    itself up without the test wiring an id through the harness.
    """
    observed: dict[str, NodeStatus] = {}

    async def check(spec: RunSpec) -> None:
        async with database.session() as db_session:
            node = await Repository(db_session).get_node(spec.cwd.name)
        assert node is not None
        observed[spec.prompt] = node.status

    harness = FakeHarness(on_start=check)
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=True
    )

    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    assert observed == {"a": NodeStatus.RUNNING, "b": NodeStatus.RUNNING}


async def test_the_write_order_holds_with_two_runs_interleaved(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """`docs/architecture.md` §4, per run, with several runs in flight.

    Sampled at broadcast time, as B3's test does: every event must already be
    a line in *its own* ``events.ndjson`` and already applied to *its own* run
    row. Two runs sharing a database and a broadcast callback must not be able
    to make either statement false for the other.
    """
    observed: list[tuple[str, str, int]] = []

    async def observe(event: AgentEvent) -> None:
        observed.append(
            (
                event.run_id,
                event.type,
                log_lines(settings.runs_root / event.run_id / "events.ndjson"),
            )
        )

    harness = FakeHarness(probe=ConcurrencyProbe(rendezvous={"x", "y"}))
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        broadcast=observe,
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("x", "y"), auto_merge=True
    )

    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    per_run: dict[str, list[tuple[str, int]]] = {}
    for run_id, event_type, lines in observed:
        per_run.setdefault(run_id, []).append((event_type, lines))
    assert len(per_run) == 2
    for entries in per_run.values():
        # Each event was durable, in its own log, before it was broadcast.
        assert [lines for _, lines in entries] == list(range(1, len(entries) + 1))
        assert [event for event, _ in entries] == [
            "run_started",
            "usage",
            "turn_finished",
            "run_finished",
        ]

    # ...and the two really were interleaved rather than run back to back: the
    # other run's first event landed before this one's last.
    sequence = [run_id for run_id, _, _ in observed]
    first = sequence[0]
    other = next(run_id for run_id in sequence if run_id != first)
    other_started = sequence.index(other)
    first_finished = len(sequence) - 1 - sequence[::-1].index(first)
    assert other_started < first_finished


async def test_a_skipped_parent_satisfies_its_dependent_without_a_branch(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """`design.md` §9: a skip must not block everything downstream.

    A skipped node never ran, so it has no branch — and a child created off a
    branch that does not exist dies in ``git worktree add``. Its parents are
    filtered to the ones that produced something.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=True
    )
    async with database.session() as db_session:
        await Repository(db_session).set_node_status(
            graph.ids_by_name["a"], NodeStatus.SKIPPED
        )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.COMPLETE
    assert result.succeeded
    assert await statuses(database, graph) == {
        "a": NodeStatus.SKIPPED,
        "b": NodeStatus.DONE,
    }
    assert harness.probe.started == ["b"]


# ---------------------------------------------------------------------------
# Graphs that are not DAGs
# ---------------------------------------------------------------------------


async def test_a_proposed_cycle_is_refused_before_a_single_row_is_written(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=FakeHarness(),
    )

    with pytest.raises(InvalidGraphError) as raised:
        await service.create_graph(
            repo_path=target_repo, nodes=plan("a:b", "b:a"), auto_merge=True
        )

    assert [error.kind for error in raised.value.errors] == [DagErrorKind.CYCLE]
    assert await service.list_sessions() == ()


async def test_an_unknown_dependency_names_the_planners_own_slug(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=FakeHarness(),
    )

    with pytest.raises(InvalidGraphError) as raised:
        await service.create_graph(
            repo_path=target_repo, nodes=plan("a", "b:typo"), auto_merge=True
        )

    error = raised.value.errors[0]
    assert error.kind is DagErrorKind.UNKNOWN_DEPENDENCY
    assert error.nodes[1] == "typo"


async def test_a_cycle_persisted_behind_the_authoring_path_is_a_typed_error(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """No database constraint can see a cycle, so the scheduler still checks."""
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=FakeHarness(),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=True
    )
    async with database.session() as db_session:
        await Repository(db_session).add_dependency(
            graph.ids_by_name["a"], graph.ids_by_name["b"]
        )

    with pytest.raises(InvalidGraphError) as raised:
        await scheduler_for(service, database, settings).run_graph(graph.session.id)

    assert [error.kind for error in raised.value.errors] == [DagErrorKind.CYCLE]
