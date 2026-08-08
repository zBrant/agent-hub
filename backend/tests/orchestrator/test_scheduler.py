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
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
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
    UsageSource,
)
from app.models.clock import now_ms
from app.models.ids import NodeId, RunId, new_run_id
from app.models.pricing import PriceTable, load_price_table
from app.models.status import NodeStatus, RunState, SessionStatus
from app.orchestrator.graph import DagErrorKind, GraphOutcome
from app.orchestrator.scheduler import GraphScheduler
from app.orchestrator.service import (
    REVIEW_FEEDBACK_HEADER,
    CreatedGraph,
    CriterionOutcome,
    InvalidGraphError,
    InvalidTransitionError,
    NodeExecution,
    NodeLimit,
    NodeLimits,
    NodeRunService,
    OrphanResolution,
    PlannedNode,
    ProcessLiveness,
    ProcessReaper,
    ResourceNotFoundError,
    ReviewDecision,
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


@dataclass(frozen=True, slots=True)
class UsageBeat:
    """One ``Usage`` event in a node's script.

    The four fields of invariant 3 are given separately rather than as a total,
    so a test can put the tokens where the real world puts them — 90%+ in
    ``cache_read`` — and a budget that counted only ``input_tokens`` would then
    be short by two orders of magnitude and would not fire.
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    source: UsageSource = "reported"

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_write

    def event(self, spec: RunSpec, ts: int) -> Usage:
        return Usage(
            run_id=spec.run_id,
            ts=ts,
            model=spec.model or MODEL,
            source=self.source,
            input_tokens=self.input,
            output_tokens=self.output,
            cache_read_tokens=self.cache_read,
            cache_write_tokens=self.cache_write,
        )


#: What every node emits unless a test scripts it otherwise.
DEFAULT_BEATS: tuple[UsageBeat, ...] = (
    UsageBeat(input=10, output=20, cache_read=30, cache_write=40),
)


def heavy(
    count: int, *, each: int, source: UsageSource = "reported"
) -> tuple[UsageBeat, ...]:
    """``count`` beats of ``each`` four-field tokens, weighted like a real run.

    1% input, 4% output, 5% cache write, the rest cache read. The exact split
    does not matter; that ``input`` is a rounding error of the total does — it
    is what makes a four-field budget and an ``input_tokens`` budget answer
    differently on the same stream.
    """
    beats: list[UsageBeat] = []
    for _ in range(count):
        low = each // 100
        out = each // 25
        write = each // 20
        beats.append(
            UsageBeat(
                input=low,
                output=out,
                cache_write=write,
                cache_read=each - low - out - write,
                source=source,
            )
        )
    return tuple(beats)


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
    # Nodes whose agent emits nothing after `RunStarted` and never exits on its
    # own. Only a kill ends the stream, which is the shape a wall-clock timeout
    # exists for and the one a token budget can never see.
    hanging: frozenset[str] = frozenset()
    usage: Mapping[str, Sequence[UsageBeat]] = field(default_factory=dict)
    on_start: Callable[[RunSpec], object] | None = None
    name: str = HARNESS
    adapters: list[FakeAdapter] = field(default_factory=list)
    # Only the adapters that actually launched a run. `create_session` and
    # `create_graph` each build one per node just to read `supported_models`,
    # so `adapters[0]` is a validation throwaway, not the one that streamed.
    launched: list[FakeAdapter] = field(default_factory=list)
    # The `RunSpec` of every launch, in order. This is where the prompt the
    # harness was actually given can be read: `meta.json` deliberately records
    # argv and not the prompt, because argv is visible in `ps`
    # (`docs/conventions.md` §6), so the spec is the only honest witness for
    # C7's "the rejection reached the next attempt".
    specs: list[RunSpec] = field(default_factory=list)

    def beats(self, node: str) -> Sequence[UsageBeat]:
        return self.usage.get(node, DEFAULT_BEATS)

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
        self.emitted: list[str] = []
        # Stands in for the real adapters' SIGTERM → grace → SIGKILL on the
        # process group: whatever the mechanism, its observable contract is that
        # the stream stops and closes with an `interrupted` RunFinished.
        self._killed = asyncio.Event()
        self._harness = harness

    def build_argv(self, spec: RunSpec) -> list[str]:
        return [*spec.launcher, self.name, "--fake-json"]

    async def start(self, spec: RunSpec) -> RunHandle:
        # The node's name is the *first line* of the prompt, not all of it: a
        # retry after a rejection is launched with the authored prompt plus the
        # reviewer's feedback appended, and the script is still keyed on the
        # node (`plan` writes the name as the first line and nothing else).
        node = spec.prompt.split("\n", 1)[0]
        self._harness.launched.append(self)
        self._harness.specs.append(spec)
        if node in self._harness.crashing:
            raise RuntimeError(f"launcher exploded for {node}")
        if self._harness.on_start is not None:
            result = self._harness.on_start(spec)
            if asyncio.iscoroutine(result):
                await result
        # One file per node, so two parallel nodes do not conflict by accident;
        # a test that wants a conflict writes the same name on purpose.
        #
        # The run id is in the content because a *retry* must produce a
        # different diff. An agent that rewrites the same bytes leaves the
        # checkpoint with nothing to commit, and `evaluate_run` then blocks the
        # node with `no_changes` — correct behaviour, and not what a test about
        # the review gate is trying to observe.
        (spec.cwd / f"{node}.txt").write_text(
            f"{node}\n{spec.run_id}\n", encoding="utf-8"
        )
        return cast(RunHandle, FakeHandle(spec=spec, node=node))

    async def send(self, handle: RunHandle, text: str) -> None:
        raise NotImplementedError

    async def interrupt(self, handle: RunHandle) -> None:
        raise NotImplementedError

    async def kill(self, handle: RunHandle) -> None:
        self.killed = True
        self._killed.set()

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
        self.emitted.append("run_started")
        await self._harness.probe.enter(node)
        try:
            if node in self._harness.hanging:
                await self._killed.wait()
                yield self._interrupted(spec, 1_030)
                self.emitted.append("run_finished")
                return
            for index, beat in enumerate(self._harness.beats(node)):
                yield beat.event(spec, 1_010 + index)
                self.emitted.append("usage")
                # The consumer ingests and decides between this line and the
                # next, so a kill triggered by the event just yielded is
                # visible here — same as a real process losing its stdout.
                if self._killed.is_set():
                    yield self._interrupted(spec, 1_030)
                    self.emitted.append("run_finished")
                    return
            yield TurnFinished(
                run_id=spec.run_id,
                ts=1_020,
                turn=1,
                status="failed" if failed else "success",
            )
            self.emitted.append("turn_finished")
            yield RunFinished(
                run_id=spec.run_id,
                ts=1_030,
                status="failed" if failed else "success",
                exit_code=1 if failed else 0,
            )
            self.emitted.append("run_finished")
        finally:
            self._harness.probe.leave(node)

    @staticmethod
    def _interrupted(spec: RunSpec, ts: int) -> RunFinished:
        return RunFinished(
            run_id=spec.run_id, ts=ts, status="interrupted", exit_code=None
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def reaper_reporting(
    liveness: ProcessLiveness, *, dies_on_sigterm: bool = True
) -> ProcessReaper:
    """A :class:`ProcessReaper` with the syscalls replaced by an answer.

    ``probe_process`` and ``terminate_process_group`` are covered against real
    processes further down; everything else needs to say "this pid was gone" or
    "this pid would not die" without arranging either.
    """
    state = {"liveness": liveness}

    def probe(pid: int) -> ProcessLiveness:
        return state["liveness"]

    def terminate(pid: int) -> None:
        if dies_on_sigterm:
            state["liveness"] = ProcessLiveness.GONE

    return ProcessReaper(probe=probe, terminate=terminate, grace_s=0.05, poll_s=0.01)


#: The overwhelmingly common case, and the default for tests that are not about
#: recovery: whatever pid a previous process recorded, nothing is there now.
GONE_REAPER = reaper_reporting(ProcessLiveness.GONE)


def build_service(
    *,
    database: Database,
    settings: Settings,
    prices: PriceTable,
    harness: FakeHarness,
    broadcast: Callable[[AgentEvent], object] | None = None,
    limits: NodeLimits | None = None,
    reaper: ProcessReaper | None = None,
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
        # Off unless a test asks for them. A cutoff that fired in the middle of
        # an unrelated assertion would be the worst kind of flake, and the tests
        # that care set the exact number they mean.
        limits=NodeLimits() if limits is None else limits,
        reaper=GONE_REAPER if reaper is None else reaper,
    )


def plan(
    *specs: str, criteria: Mapping[str, Sequence[str]] | None = None
) -> tuple[PlannedNode, ...]:
    """``"d:b,c"`` is a node named ``d`` that depends on ``b`` and ``c``.

    The prompt is the node's name, which is what the fake adapter keys its
    script on and what the file it writes is called.

    ``criteria`` gives a node the prose `design.md` §8's planner emits. It is
    prose on purpose in every test below — "pytest tests/test_auth.py passes"
    *describes* a command and is not one, which is the whole reason §9 hands
    them to a human instead of running them.
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
                acceptance_criteria=tuple((criteria or {}).get(name, ())),
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


async def runs_of(database: Database, node_id: NodeId) -> list[tuple[RunState, int]]:
    """``(status, four-field token total)`` per attempt at ``node_id``."""
    async with database.session() as db_session:
        repository = Repository(db_session)
        rows = await repository.list_runs(node_id)
        return [
            (
                run.status,
                (await repository.usage_totals(run_id=run.id)).counts.total,
            )
            for run in rows
        ]


async def usage_sources(database: Database, node_id: NodeId) -> list[str]:
    async with database.session() as db_session:
        repository = Repository(db_session)
        rows = await repository.list_runs(node_id)
        events = [
            event for run in rows for event in await repository.list_usage(run.id)
        ]
    return [str(event.source) for event in events]


async def merged_into_integration(
    database: Database, graph: CreatedGraph, name: str
) -> bool:
    """Is this node's branch an ancestor of the integration branch?

    The question invariant 6 is actually about. Asserting that a file the agent
    wrote is absent from the integration worktree is weaker in two ways: a node
    whose agent wrote nothing would pass it trivially, and a merge that landed
    but was never checked out would sneak past it. ``merge-base --is-ancestor``
    asks git whether the commit is in there.
    """
    async with database.session() as db_session:
        node = await Repository(db_session).get_node(graph.ids_by_name[name])
    assert node is not None and node.branch is not None
    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(graph.session.workspace_root / "integration"),
        "merge-base",
        "--is-ancestor",
        node.branch,
        "HEAD",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return await process.wait() == 0


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

        async def recover_orphans(self, session_id: str) -> Sequence[OrphanResolution]:
            return ()

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
    # ...and not merely absent from the checkout: the commit is not in the
    # integration branch's history at all.
    assert await merged_into_integration(database, graph, "a") is False
    async with database.session() as db_session:
        session = await Repository(db_session).get_session(graph.session.id)
    assert session is not None and session.status is SessionStatus.PAUSED

    # The gate opens and the graph continues from where it stopped.
    await service.approve_node(graph.ids_by_name["a"])
    assert await merged_into_integration(database, graph, "a") is True
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


# ---------------------------------------------------------------------------
# C6 — per-node budgets and wall-clock timeouts
# ---------------------------------------------------------------------------


def test_the_per_node_cutoffs_are_on_by_default(tmp_path: Path) -> None:
    """`design.md` §12 lists the runaway agent as a risk *mitigated*.

    A mitigation that ships disabled is a comment. Both cutoffs are generous
    enough that a real node does not meet them and finite enough that a loop
    does.
    """
    defaults = Settings(root=tmp_path)
    assert defaults.node_token_budget == 50_000_000
    assert defaults.node_timeout_s == 3600.0
    assert NodeLimits.from_settings(defaults) == NodeLimits(
        token_budget=50_000_000, wall_clock_s=3600.0
    )


async def test_a_token_budget_kills_a_running_node_mid_stream(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Killed, ``failed``, and the tokens it already burned are still recorded.

    Ten beats of 1000 four-field tokens are scripted; the budget is 3500. The
    kill has to land after the fourth beat — the first one that puts the total
    over — and the stream has to stop there rather than run to completion, so
    the assertion is on both the persisted usage and how much of the script the
    adapter ever got to emit.
    """
    harness = FakeHarness(usage={"a": heavy(10, each=1_000)})
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        limits=NodeLimits(token_budget=3_500),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    node_id = graph.ids_by_name["a"]
    assert await statuses(database, graph) == {"a": NodeStatus.FAILED}
    # The run is `interrupted`, exactly as for an operator kill: that is what
    # the log says, so that is what the projection may say (invariant 4).
    assert await runs_of(database, node_id) == [(RunState.INTERRUPTED, 4_000)]

    execution = result.executions[node_id]
    assert execution.outcome is not None
    assert execution.outcome.limit is NodeLimit.TOKEN_BUDGET
    assert execution.outcome.merge is None

    adapter = harness.launched[0]
    assert adapter.killed
    # Mid-stream, not after the fact: four of ten beats, and no `turn_finished`.
    assert adapter.emitted == ["run_started", *["usage"] * 4, "run_finished"]
    assert not (graph.session.workspace_root / "integration" / "a.txt").exists()


async def test_the_budget_counts_all_four_fields_and_not_just_input(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Invariant 3, as a cutoff rather than as a dashboard number.

    ``heavy`` puts 1% of each beat in ``input_tokens``, which is roughly where
    a real session puts it. Five beats of 10 000 tokens is 50 000 four-field
    tokens and 500 input tokens. The budget is 20 000: over the four-field
    total, and 40x over the input-only one. A check that summed
    ``input_tokens`` would run the whole script to completion and merge the
    node.
    """
    harness = FakeHarness(usage={"a": heavy(5, each=10_000)})
    assert sum(beat.input for beat in harness.beats("a")) == 500
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        limits=NodeLimits(token_budget=20_000),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )

    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    assert await statuses(database, graph) == {"a": NodeStatus.FAILED}
    assert await runs_of(database, graph.ids_by_name["a"]) == [
        (RunState.INTERRUPTED, 30_000)
    ]
    assert harness.launched[0].emitted.count("usage") == 3


async def test_the_budget_fires_on_reconstructed_usage(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Phase 0's A3 shape, which is the one the real world produces.

    A budget-exhausted Claude Code turn reports ``result.usage`` as all zeros
    and B3 rebuilds the real numbers from the cumulative ``modelUsage``,
    emitting them as ``source="reconstructed"``. So the stream here opens with a
    zeroed ``reported`` beat and continues with reconstructed ones. An
    implementation that trusted the harness's own total, or filtered on
    ``source``, reads zero and never fires.
    """
    harness = FakeHarness(
        usage={
            "a": (
                UsageBeat(source="reported"),
                *heavy(4, each=5_000, source="reconstructed"),
            )
        }
    )
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        limits=NodeLimits(token_budget=9_000),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )

    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    node_id = graph.ids_by_name["a"]
    assert await statuses(database, graph) == {"a": NodeStatus.FAILED}
    assert await runs_of(database, node_id) == [(RunState.INTERRUPTED, 10_000)]
    # The zeroed self-report is kept alongside the reconstruction, and it is the
    # reconstruction the cutoff read.
    assert await usage_sources(database, node_id) == [
        "reported",
        "reconstructed",
        "reconstructed",
    ]


async def test_a_wall_clock_timeout_kills_a_node_that_produces_no_usage(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """The case a token budget structurally cannot see.

    The agent starts, emits nothing and never exits. No ``Usage`` event ever
    arrives, so nothing accumulates and nothing crosses a threshold; only a
    clock can end this. The test hangs rather than fails if the watchdog is
    removed, which is the honest failure mode for a timeout.
    """
    harness = FakeHarness(hanging=frozenset({"a"}))
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        limits=NodeLimits(token_budget=1_000_000, wall_clock_s=0.2),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )

    result = await asyncio.wait_for(
        scheduler_for(service, database, settings).run_graph(graph.session.id),
        timeout=20,
    )

    node_id = graph.ids_by_name["a"]
    assert await statuses(database, graph) == {"a": NodeStatus.FAILED}
    assert await runs_of(database, node_id) == [(RunState.INTERRUPTED, 0)]
    execution = result.executions[node_id]
    assert execution.outcome is not None
    assert execution.outcome.limit is NodeLimit.WALL_CLOCK
    assert harness.launched[0].emitted == ["run_started", "run_finished"]


async def test_neither_cutoff_fires_on_a_normal_run(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """The limit must not be accidentally always-on.

    Both scenarios run the identical stream — one beat of exactly 100
    four-field tokens. The first gives it a budget of exactly 100 and a wall
    clock of an hour and must run to completion and merge; the second gives it
    99 and must not. One token of difference is the whole experiment, so a
    cutoff with an off-by-one, an inverted comparison or a hardcoded ``True``
    fails one side or the other.
    """
    outcomes: dict[int, tuple[NodeStatus, list[str]]] = {}
    for budget in (100, 99):
        harness = FakeHarness()
        service = build_service(
            database=database,
            settings=settings,
            prices=prices,
            harness=harness,
            limits=NodeLimits(token_budget=budget, wall_clock_s=3600.0),
        )
        graph = await service.create_graph(
            repo_path=target_repo, nodes=plan("a"), auto_merge=True
        )
        await scheduler_for(service, database, settings).run_graph(graph.session.id)
        node_status = (await statuses(database, graph))["a"]
        outcomes[budget] = (node_status, harness.launched[0].emitted)

    assert outcomes[100] == (
        NodeStatus.DONE,
        ["run_started", "usage", "turn_finished", "run_finished"],
    )
    assert outcomes[99] == (
        NodeStatus.FAILED,
        ["run_started", "usage", "run_finished"],
    )


async def test_a_budget_killed_node_blocks_its_dependents_and_spares_a_sibling(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """A cutoff is an ordinary terminal outcome, so it propagates like one.

    ``b`` never launches because ``a`` was cut off; ``c`` is on an unrelated
    branch and lands in integration regardless. Nothing about being killed for a
    budget travels further than the node it killed.
    """
    harness = FakeHarness(usage={"a": heavy(20, each=1_000)})
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        limits=NodeLimits(token_budget=2_500),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a", "c"), auto_merge=True
    )

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    ids = graph.ids_by_name
    assert result.outcome is GraphOutcome.WAITING_ON_HUMAN
    assert await statuses(database, graph) == {
        "a": NodeStatus.FAILED,
        "b": NodeStatus.BLOCKED,
        "c": NodeStatus.DONE,
    }
    assert result.blocked_by(ids["b"]) == (ids["a"],)
    assert await run_counts(database, graph) == {"a": 1, "b": 0, "c": 1}

    integration = graph.session.workspace_root / "integration"
    assert (integration / "c.txt").exists()
    assert not (integration / "a.txt").exists()

    # ...and the partial work is on the node's own branch, not thrown away: B7's
    # retry has something to start from and a human has something to read.
    committed = await git(
        await worktree_of(database, ids["a"]), "log", "--oneline", "--", "a.txt"
    )
    assert committed.strip()


# ---------------------------------------------------------------------------
# Restart recovery
# ---------------------------------------------------------------------------


async def strand_a_running_run(
    database: Database, node_id: NodeId, *, pid: int | None = 424_242
) -> RunId:
    """Leave behind exactly what a killed orchestrator leaves behind.

    A ``run`` row still ``RUNNING`` with a pid, and its node still ``running``.
    Written through the repository rather than raw SQL so the row is the shape
    the projection really produces — including ``events_path``, which recovery
    must not need to read.
    """
    async with database.session() as db_session:
        repository = Repository(db_session)
        run = await repository.create_run(
            run_id=new_run_id(),
            node_id=node_id,
            events_path=Path("/nonexistent/events.ndjson"),
        )
        await repository.start_run(
            run.id,
            RunStarted(
                run_id=run.id,
                ts=now_ms(),
                harness=HARNESS,
                model=MODEL,
                cwd=Path("/nonexistent"),
                pid=pid,
            ),
        )
        await repository.set_node_status(node_id, NodeStatus.RUNNING)
    return run.id


async def test_a_run_left_running_by_a_dead_process_is_resolved_on_the_next_tick(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """The restart case, end to end, with no human in it.

    The previous orchestrator died mid-run: its row says ``running`` and its
    node says ``running``, so nothing is startable and — without recovery — the
    node is stuck there forever. The sweep closes the row and makes the node
    actionable again.

    It does **not** restart it. A recovered node lands in ``failed``, and the
    scheduler drives past ``failed`` no more than it invents a retry anywhere
    else: an attempt is an operator decision (B7), and a graph that silently
    re-ran everything it found half-finished would spend tokens nobody asked
    for. So the assertion is that a human now *can* act, not that the machine
    already did.

    Built on ``create_session``, which materializes eagerly and leaves the node
    ``ready``. That is the reachable shape of a crash during a first attempt: a
    ``running`` row cannot exist without a worktree, because ``_prepare``
    persists ``attach_worktree`` before ``create_run`` opens the attempt.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    created = await service.create_session(
        repo_path=target_repo, prompt="a", harness=HARNESS, model=MODEL
    )
    node_id = created.node.id
    stranded = await strand_a_running_run(database, node_id)

    (resolution,) = await service.recover_orphans(created.session.id)

    assert resolution.run_id == stranded
    assert resolution.liveness is ProcessLiveness.GONE
    assert resolution.node_status is NodeStatus.FAILED
    # Closed, not adopted: the pipes died with the parent that opened them, so
    # no further AgentEvent can ever arrive on this run.
    assert [state for state, _ in await runs_of(database, node_id)] == [
        RunState.INTERRUPTED
    ]

    # Actionable: an explicit retry opens a *new* attempt and finishes it.
    await service.retry_node(node_id)

    assert [state for state, _ in await runs_of(database, node_id)] == [
        RunState.INTERRUPTED,
        RunState.SUCCESS,
    ]


async def test_recovery_keeps_the_worktree_of_a_half_finished_node(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """A partially completed node's diff is what a human needs to decide.

    So the sweep may close the run, but it may not touch the worktree, the
    branch, or anything already written.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    worktree = await worktree_of(database, node_id)
    before = sorted(path.name for path in worktree.iterdir())
    await strand_a_running_run(database, node_id)

    await service.recover_orphans(graph.session.id)

    assert worktree.is_dir()
    assert sorted(path.name for path in worktree.iterdir()) == before


async def test_a_leftover_process_that_survives_sigterm_blocks_instead_of_failing(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Still-alive means ``blocked``, and blocked means the scheduler stops.

    ``failed`` is retryable and the scheduler will drive past it on its own —
    which would start a second agent in a worktree the first one is still
    writing to, corrupting exactly the diff invariant 2 exists to protect.
    """
    harness = FakeHarness()
    service = build_service(
        database=database,
        settings=settings,
        prices=prices,
        harness=harness,
        reaper=reaper_reporting(ProcessLiveness.ALIVE, dies_on_sigterm=False),
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )
    node_id = graph.ids_by_name["a"]
    stranded = await strand_a_running_run(database, node_id)

    (resolution,) = await service.recover_orphans(graph.session.id)

    assert resolution.run_id == stranded
    assert resolution.liveness is ProcessLiveness.ALIVE
    assert resolution.terminated is False
    assert resolution.node_status is NodeStatus.BLOCKED
    assert await statuses(database, graph) == {"a": NodeStatus.BLOCKED}


async def test_the_sweep_leaves_this_process_own_runs_alone(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """The dangerous false positive: a sweep that reaps a live local run.

    ``recover_orphans`` runs at startup, but nothing stops it running while a
    node is streaming. A row this process owns is not an orphan, and mistaking
    one for an orphan would kill a healthy agent.
    """
    harness = FakeHarness(probe=ConcurrencyProbe(hold=True))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=True
    )

    running = asyncio.create_task(
        scheduler_for(service, database, settings).run_graph(graph.session.id)
    )
    try:
        async with asyncio.timeout(20):
            await harness.probe.wait_for_census(1)
        assert await service.recover_orphans(graph.session.id) == ()
    finally:
        harness.probe.release()

    result = await running

    assert result.outcome is GraphOutcome.COMPLETE
    assert harness.launched[0].killed is False


# ---------------------------------------------------------------------------
# C7 — acceptance criteria and the human gate
# ---------------------------------------------------------------------------


async def checklist(
    service: NodeRunService, node_id: NodeId
) -> list[tuple[int, int, str, CriterionOutcome]]:
    """``(attempt, position, criterion, outcome)`` for every judgement."""
    return [
        (row.attempt, row.position, row.criterion, row.outcome)
        for row in await service.acceptance_results(node_id)
    ]


async def verdicts(
    service: NodeRunService, node_id: NodeId
) -> list[tuple[int, ReviewDecision, str | None]]:
    return [
        (row.attempt, row.decision, row.feedback)
        for row in await service.reviews(node_id)
    ]


CRITERIA = (
    "pytest tests/test_auth.py passes",
    "the OpenAPI schema is regenerated",
    "no new TODO comments",
)


async def test_each_criterion_carries_its_own_outcome(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """`docs/phase-2.md` C7: three criteria can be two-pass one-fail.

    Which is the whole reason C1 made the column an array. A single joined
    string could record "the reviewer was unhappy" and nothing about *which*
    promise was broken, and `design.md` §8's panel shows results, plural.

    Note what the run itself did: it recorded three ``unevaluated`` rows. It
    did not try to run "pytest tests/test_auth.py passes", because that is
    prose describing a command (`design.md` §9).
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo,
        nodes=plan("a", criteria={"a": CRITERIA}),
        auto_merge=False,
    )
    node_id = graph.ids_by_name["a"]

    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    assert (await statuses(database, graph))["a"] is NodeStatus.AWAITING_REVIEW
    assert await checklist(service, node_id) == [
        (1, 0, CRITERIA[0], CriterionOutcome.UNEVALUATED),
        (1, 1, CRITERIA[1], CriterionOutcome.UNEVALUATED),
        (1, 2, CRITERIA[2], CriterionOutcome.UNEVALUATED),
    ]
    assert await verdicts(service, node_id) == []

    await service.approve_node(
        node_id,
        outcomes={
            0: CriterionOutcome.PASS,
            1: CriterionOutcome.FAIL,
            2: CriterionOutcome.PASS,
        },
    )

    assert await checklist(service, node_id) == [
        (1, 0, CRITERIA[0], CriterionOutcome.PASS),
        (1, 1, CRITERIA[1], CriterionOutcome.FAIL),
        (1, 2, CRITERIA[2], CriterionOutcome.PASS),
    ]
    # Approved anyway, with a criterion failing. The reviewer decides what is
    # disqualifying; refusing here would teach them to leave the list blank.
    assert await verdicts(service, node_id) == [(1, ReviewDecision.APPROVED, None)]
    assert (await statuses(database, graph))["a"] is NodeStatus.DONE
    assert await merged_into_integration(database, graph, "a") is True


async def test_auto_merge_on_records_the_criteria_and_merges_anyway(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """`design.md` §9's stated limitation, asserted so nobody mistakes it for a bug.

    With ``auto_merge`` on there is no reviewer, so nothing can resolve the
    checklist. The criteria are still recorded — attempt 1 was merged with
    *these* promises outstanding, and that stays true after someone edits the
    node — and the merge happens on the harness's own verdict.

    The rows read ``unevaluated`` forever, and the absence of a
    :class:`NodeReview` row is what says no human was ever involved. That is
    the whole shape of the limitation, in data.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo,
        nodes=plan("a", "b:a", criteria={"a": CRITERIA[:2]}),
        auto_merge=True,
    )
    node_id = graph.ids_by_name["a"]

    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.COMPLETE
    assert await statuses(database, graph) == dict.fromkeys("ab", NodeStatus.DONE)
    assert await merged_into_integration(database, graph, "a") is True
    assert await checklist(service, node_id) == [
        (1, 0, CRITERIA[0], CriterionOutcome.UNEVALUATED),
        (1, 1, CRITERIA[1], CriterionOutcome.UNEVALUATED),
    ]
    assert await verdicts(service, node_id) == []


async def test_a_rejection_opens_a_new_run_carrying_the_feedback(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """C7's done-when: the rejection text reaches the retry's prompt.

    Asserted on the ``RunSpec`` the adapter was handed, not on ``meta.json``.
    ``meta.json`` records argv and deliberately not the prompt, because argv is
    visible in ``ps`` (`docs/conventions.md` §6) — so for a real harness there
    is nothing about the prompt in that file to assert on. C7's done-when says
    "argv or prompt"; the prompt is the only one of the two that can be true.
    """
    feedback = "the endpoint still returns 500 for an expired token"
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo,
        nodes=plan("a", criteria={"a": CRITERIA[:1]}),
        auto_merge=False,
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)
    first = (await service.acceptance_results(node_id))[0]
    assert first.outcome is CriterionOutcome.UNEVALUATED
    # The first attempt was launched with exactly what the operator authored.
    assert harness.specs[0].prompt == "a"

    outcome = await service.reject_node(
        node_id, feedback=feedback, outcomes={0: CriterionOutcome.FAIL}
    )

    # A new Run, never a mutated one (B7).
    assert [state for state, _ in await runs_of(database, node_id)] == [
        RunState.SUCCESS,
        RunState.SUCCESS,
    ]
    assert outcome.run_id != harness.specs[0].run_id
    retried = harness.specs[-1]
    assert retried.prompt.startswith("a\n")
    assert feedback in retried.prompt
    assert REVIEW_FEEDBACK_HEADER in retried.prompt
    assert "### Attempt 1" in retried.prompt

    # The verdict is attached to the attempt it judged, and the new attempt
    # starts its own checklist rather than inheriting the old verdict.
    assert await checklist(service, node_id) == [
        (1, 0, CRITERIA[0], CriterionOutcome.FAIL),
        (2, 0, CRITERIA[0], CriterionOutcome.UNEVALUATED),
    ]
    assert await verdicts(service, node_id) == [(1, ReviewDecision.REJECTED, feedback)]
    # Rejected work is not merged, and the node is back at the gate.
    assert (await statuses(database, graph))["a"] is NodeStatus.AWAITING_REVIEW
    assert await merged_into_integration(database, graph, "a") is False


async def test_a_rejection_never_edits_the_authored_prompt(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """``node.prompt`` is authored input, and a run may not rewrite it.

    The feedback is composed on top at launch. Persisting the composition would
    destroy what the operator wrote and make the third attempt's prompt a
    function of how many times the second one was retried.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=False
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    await service.reject_node(node_id, feedback="not what I asked for")

    async with database.session() as db_session:
        node = await Repository(db_session).get_node(node_id)
    assert node is not None and node.prompt == "a"


async def test_rejecting_twice_accumulates_every_earlier_rejection(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Pinned: attempt 3 sees attempt 1's objection *and* attempt 2's.

    Accumulate rather than replace. An agent shown only the newest complaint
    fixes it by undoing the fix for the oldest, and the reviewer would
    otherwise have to restate every surviving objection every time — work the
    record has already done.

    The header appears once and the attempts appear in order, so the agent can
    tell which objection came from which round.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=False
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    await service.reject_node(node_id, feedback="the tests are still skipped")
    await service.reject_node(node_id, feedback="now the migration is missing")

    third = harness.specs[-1].prompt
    assert third.count(REVIEW_FEEDBACK_HEADER) == 1
    assert third.index("the tests are still skipped") < third.index(
        "now the migration is missing"
    )
    assert third.index("### Attempt 1") < third.index("### Attempt 2")
    assert await verdicts(service, node_id) == [
        (1, ReviewDecision.REJECTED, "the tests are still skipped"),
        (2, ReviewDecision.REJECTED, "now the migration is missing"),
    ]
    assert [state for state, _ in await runs_of(database, node_id)] == [
        RunState.SUCCESS
    ] * 3


async def test_rejecting_without_saying_why_is_refused(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """The retry's only new input is the reviewer's words.

    An invalid argument, so an exception (`docs/architecture.md` §9) — and it
    happens before anything is persisted or launched.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=False
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    with pytest.raises(ValueError, match="requires feedback"):
        await service.reject_node(node_id, feedback="   ")

    assert (await statuses(database, graph))["a"] is NodeStatus.AWAITING_REVIEW
    assert await verdicts(service, node_id) == []
    assert len(harness.specs) == 1


async def test_a_node_awaiting_review_cannot_be_re_run_behind_the_gate(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """Invariant 6 again, from the other side.

    ``retry_node`` is the "run it again" verb and it does not accept a node a
    human is in the middle of reviewing: leaving ``awaiting_review`` is the
    gate's decision, and a bare retry would silently replace the diff under the
    reviewer without recording that anyone rejected anything.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=False
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    with pytest.raises(InvalidTransitionError, match="only failed or blocked"):
        await service.retry_node(node_id)

    assert (await statuses(database, graph))["a"] is NodeStatus.AWAITING_REVIEW
    assert len(harness.specs) == 1


async def test_a_retry_after_a_failure_can_carry_feedback_too(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """One path, two entry points.

    A failed node never reached the gate, so nothing rejected it — but an
    operator retrying it by hand has exactly the same thing to say, and it has
    to reach the prompt by the same route. ``reject_node`` is
    ``retry_node`` with the node state the gate produces.
    """
    harness = FakeHarness(failing=frozenset({"a"}))
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a"), auto_merge=False
    )
    node_id = graph.ids_by_name["a"]
    await scheduler_for(service, database, settings).run_graph(graph.session.id)
    assert (await statuses(database, graph))["a"] is NodeStatus.FAILED

    harness.failing = frozenset()
    await service.retry_node(node_id, feedback="you deleted the wrong module")

    assert "you deleted the wrong module" in harness.specs[-1].prompt
    assert await verdicts(service, node_id) == [
        (1, ReviewDecision.REJECTED, "you deleted the wrong module")
    ]
    assert (await statuses(database, graph))["a"] is NodeStatus.AWAITING_REVIEW


async def test_the_gate_holds_the_dependents_of_a_rejected_node(
    database: Database,
    settings: Settings,
    prices: PriceTable,
    target_repo: Path,
) -> None:
    """A rejected node never satisfied anything, so nothing downstream moves.

    The scheduler is run again after the rejection to make the point: the graph
    is still ``waiting_on_human``, ``b`` is still ``pending``, and the only
    thing that changed is that ``a`` has one more attempt behind it.
    """
    harness = FakeHarness()
    service = build_service(
        database=database, settings=settings, prices=prices, harness=harness
    )
    graph = await service.create_graph(
        repo_path=target_repo, nodes=plan("a", "b:a"), auto_merge=False
    )
    await scheduler_for(service, database, settings).run_graph(graph.session.id)

    await service.reject_node(graph.ids_by_name["a"], feedback="wrong table name")
    result = await scheduler_for(service, database, settings).run_graph(
        graph.session.id
    )

    assert result.outcome is GraphOutcome.WAITING_ON_HUMAN
    assert await statuses(database, graph) == {
        "a": NodeStatus.AWAITING_REVIEW,
        "b": NodeStatus.PENDING,
    }
    assert await run_counts(database, graph) == {"a": 2, "b": 0}
    assert await merged_into_integration(database, graph, "a") is False
