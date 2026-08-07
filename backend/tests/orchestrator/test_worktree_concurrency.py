"""Worktrees under concurrency: the shape the scheduler will actually produce.

`test_worktree.py` covers the sequential lifecycle. This module covers what
happens when several nodes are materialized at once and several finished nodes
race into the *one* integration worktree (C4 and C5 of `docs/phase-2.md`).

Everything here runs against real temp repositories for the same reason as
`test_worktree.py`: the risk is git behaving differently than assumed, and a mock
would only confirm the assumption. The two measured answers this module pins:

* concurrent ``git merge`` into one integration worktree is unsafe and loud: it
  loses commits, and one of its four failure modes exits 1, exactly like a real
  conflict. The races below are run for several iterations each because a race
  that passes once proves nothing — with the merge lock removed, every one of
  them fails on iteration 0, four of five merges lost.
* concurrent ``git worktree add`` is unsafe and *quiet*: reproducing it needs
  16-way concurrency and dozens of rounds (3 failures in 60 rounds; 0 in 60 at
  8-way). A test cannot reliably reproduce that in a few seconds, so
  ``test_worktree_registration_is_serialized_per_repository`` asserts the
  serialization directly instead of trying to provoke the bug.
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.models.ids import new_session_id
from app.models.status import NodeStatus
from app.orchestrator.worktree import (
    MergeResult,
    MergeStatus,
    NodeWorktree,
    SessionWorkspace,
    init_session_workspace,
)

# Enough repetitions that an unserialized merge is caught every time, cheap
# enough that the module stays a few seconds. Measured: with the lock removed,
# five nodes racing lose four merges on iteration 0.
RACE_NODES = 5
RACE_ITERATIONS = 4


async def git(cwd: Path, *args: str) -> str:
    code, output = await git_status(cwd, *args)
    assert code == 0, f"git {args} failed:\n{output}"
    return output


async def git_status(cwd: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
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
    stdout, _ = await proc.communicate()
    return proc.returncode or 0, stdout.decode()


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[Path]:
    path = tmp_path / "target-repo"
    path.mkdir()
    await git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("line1\nline2\n")
    await git(path, "add", "-A")
    await git(path, "commit", "-qm", "initial")
    yield path


@pytest.fixture
async def workspace(repo: Path, tmp_path: Path) -> SessionWorkspace:
    return await init_session_workspace(
        repo_path=repo,
        session_id=new_session_id(),
        workspaces_root=tmp_path / "workspaces",
    )


async def work(workspace: SessionWorkspace, node: NodeWorktree, files: int = 8) -> str:
    """A node's agent: write a few files nobody else touches, then commit.

    Several files rather than one because a wider merge widens the window the
    race has to hit, and because a lost merge is then visible as missing content
    and not only as a missing commit.
    """
    for index in range(files):
        (node.path / f"{node.node_id}_{index}.txt").write_text("body\n" * 20)
    result = await workspace.commit(node.node_id, f"feat: {node.node_id}")
    assert result.committed
    assert result.commit is not None
    return result.commit


async def merge_commits(workspace: SessionWorkspace) -> set[str]:
    listing = await git(
        workspace.integration_path,
        "log",
        "--merges",
        "--format=%H",
        workspace.integration_branch,
    )
    return set(listing.split())


@dataclass(frozen=True)
class GitCall:
    """One recorded ``git`` invocation. ``phase`` is ``start`` or ``end``: a
    single event per command cannot show that two commands *overlapped*, which
    is the only thing worth asserting about a mutex."""

    phase: str
    owner: str
    cwd: Path
    args: tuple[str, ...]


def spy_on_git(monkeypatch: pytest.MonkeyPatch, log: list[GitCall]) -> None:
    """Record the start and end of every git invocation with the task that
    issued it, yielding the loop first so that whatever *can* interleave, does."""
    original = SessionWorkspace._git

    async def recording(
        self: SessionWorkspace, cwd: Path, *args: str, **kwargs: Any
    ) -> Any:
        task = asyncio.current_task()
        owner = task.get_name() if task else "?"
        log.append(GitCall("start", owner, cwd, args))
        await asyncio.sleep(0)
        try:
            return await original(self, cwd, *args, **kwargs)
        finally:
            log.append(GitCall("end", owner, cwd, args))

    monkeypatch.setattr(SessionWorkspace, "_git", recording)


def assert_never_overlapped(calls: Sequence[GitCall]) -> list[str]:
    """No two owners hold the floor at once. Returns the owners in order."""
    in_flight: set[str] = set()
    order: list[str] = []
    for call in calls:
        if call.phase == "start":
            assert not (in_flight - {call.owner}), (
                f"{call.owner} started {' '.join(call.args[:2])} "
                f"while {in_flight} was still running"
            )
            in_flight.add(call.owner)
            if not order or order[-1] != call.owner:
                order.append(call.owner)
        else:
            in_flight.discard(call.owner)
    return order


async def assert_integration_is_clean(workspace: SessionWorkspace) -> None:
    """No half-finished merge left behind: nothing staged, nothing modified, no
    ``MERGE_HEAD``. This is the state that decides whether the *next* node can
    merge at all."""
    assert await git(workspace.integration_path, "status", "--porcelain") == ""
    code, _ = await git_status(
        workspace.integration_path, "rev-parse", "-q", "--verify", "MERGE_HEAD"
    )
    assert code != 0, "the integration worktree is still mid-merge"


# ---------------------------------------------------------------------------
# C4 — multi-node materialization
# ---------------------------------------------------------------------------


async def test_diamond_child_worktree_contains_both_parent_edits(
    workspace: SessionWorkspace,
) -> None:
    """The shape the scheduler produces: two nodes off one base, then a join."""
    root = await workspace.create_node("root")
    await work(workspace, root, files=1)

    left, right = await asyncio.gather(
        workspace.create_node("left", parents=["root"]),
        workspace.create_node("right", parents=["root"]),
    )
    assert left.base_ref == right.base_ref
    left_commit, right_commit = await asyncio.gather(
        work(workspace, left), work(workspace, right)
    )

    join = await workspace.create_node("join", parents=["left", "right"])

    assert not join.blocked
    assert [m.status for m in join.parent_merges] == [MergeStatus.MERGED]
    # Both branches' work is on disk, not just in history.
    assert (join.path / "left_0.txt").exists()
    assert (join.path / "right_0.txt").exists()
    assert (join.path / "root_0.txt").exists()
    # And both parents are genuine ancestors, so the join's diff carries them.
    for commit in (left_commit, right_commit):
        code, _ = await git_status(
            join.path, "merge-base", "--is-ancestor", commit, "HEAD"
        )
        assert code == 0, f"{commit} is not an ancestor of the join"


async def test_conflicting_parents_block_the_child_and_no_agent_is_launched(
    workspace: SessionWorkspace,
) -> None:
    """The caller sees ``blocked`` and stops — the point of folding parents at
    materialization time rather than at merge time."""
    launched: list[str] = []

    async def materialize(node_id: str, parents: Sequence[str]) -> NodeStatus:
        """What C3's scheduler does per node, reduced to the branch that matters."""
        node = await workspace.create_node(node_id, parents=parents)
        if node.blocked:
            return NodeStatus.BLOCKED
        launched.append(node_id)  # the harness would start here
        return NodeStatus.RUNNING

    for node_id, text in (("left", "from left\n"), ("right", "from right\n")):
        parent = await workspace.create_node(node_id)
        (parent.path / "a.txt").write_text(text + "line2\n")
        await workspace.commit(node_id, f"feat: {node_id}")

    assert await materialize("early", parents=["left"]) is NodeStatus.RUNNING
    status = await materialize("join", parents=["left", "right"])

    assert status is NodeStatus.BLOCKED
    assert launched == ["early"], "an agent was launched on an unbuildable base"

    join = workspace.node_path("join")
    assert await git(join, "status", "--porcelain") == ""
    code, _ = await git_status(join, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    assert code != 0, "the child worktree was left mid-merge"


async def test_nodes_materialized_at_once_all_come_out_usable(
    workspace: SessionWorkspace,
) -> None:
    """Eight nodes off one base at once — the scheduler's fan-out."""
    for iteration in range(RACE_ITERATIONS):
        ids = [f"n{iteration}_{index}" for index in range(8)]
        nodes = await asyncio.gather(*(workspace.create_node(i) for i in ids))

        registered = await workspace.list_worktrees()
        head = (await git(workspace.integration_path, "rev-parse", "HEAD")).strip()
        for node in nodes:
            assert node.path.resolve() in registered
            assert node.base_ref == head
            assert not node.blocked
            branch = await git(node.path, "rev-parse", "--abbrev-ref", "HEAD")
            assert branch.strip() == node.branch
            # Fully checked out and usable, not merely registered.
            assert (node.path / "a.txt").read_text() == "line1\nline2\n"
            assert await git(node.path, "status", "--porcelain") == ""


async def test_worktree_registration_is_serialized_per_repository(
    workspace: SessionWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git worktree add`` is *not* safe concurrently, which is not obvious: it
    creates ``.git/worktrees/<id>/`` and fills in ``commondir`` in two steps while
    another add is reading that directory, and the loser dies with
    ``fatal: failed to read .../commondir: Undefined error: 0``.

    Reproducing it takes 16-way concurrency and dozens of rounds, so this test
    pins the fix rather than the bug: no two ``worktree add`` may be in flight.
    """
    log: list[GitCall] = []
    spy_on_git(monkeypatch, log)

    ids = [f"n{index}" for index in range(6)]
    await asyncio.gather(
        *(
            asyncio.create_task(workspace.create_node(node_id), name=node_id)
            for node_id in ids
        )
    )

    adds = [call for call in log if call.args[:2] == ("worktree", "add")]
    assert sorted(assert_never_overlapped(adds)) == sorted(ids)
    # Only the add is inside the mutex — the rest of `create_node` is parallel,
    # which is visible as other nodes' commands running between two adds.
    between = [
        call
        for index, call in enumerate(log)
        if call.phase == "start"
        and call.args[:2] != ("worktree", "add")
        and any(c.args[:2] == ("worktree", "add") for c in log[index:])
        and any(c.args[:2] == ("worktree", "add") for c in log[:index])
    ]
    assert between, "create_node looks serialized as a whole, not just its add"
    assert await workspace.registry_lock() is not await workspace.integration_lock()


async def test_two_sessions_on_one_repository_share_the_registration_lock(
    repo: Path, tmp_path: Path
) -> None:
    """``.git/worktrees/`` belongs to the repository, not to the session, so the
    registration mutex has to be keyed by the repository — including when the
    same repository is reached by another name."""
    first, second = await asyncio.gather(
        init_session_workspace(
            repo_path=repo,
            session_id="sess_first",
            workspaces_root=tmp_path / "workspaces",
        ),
        init_session_workspace(
            repo_path=repo,
            session_id="sess_second",
            workspaces_root=tmp_path / "workspaces",
        ),
    )
    assert await first.registry_lock() is await second.registry_lock()
    # ...while their merges stay independent.
    assert await first.integration_lock() is not await second.integration_lock()

    link = tmp_path / "linked-repo"
    link.symlink_to(repo, target_is_directory=True)
    aliased = await init_session_workspace(
        repo_path=link,
        session_id="sess_aliased",
        workspaces_root=tmp_path / "workspaces",
    )
    assert await aliased.registry_lock() is await first.registry_lock()

    nodes = await asyncio.gather(
        *(workspace.create_node("node") for workspace in (first, second, aliased))
    )
    registered = await first.list_worktrees()
    for node in nodes:
        assert node.path.resolve() in registered


async def test_node_worktrees_can_be_created_while_another_node_merges(
    workspace: SessionWorkspace,
) -> None:
    """``worktree add`` runs with the integration worktree as its cwd, so it has
    to be safe while a merge is in flight there — otherwise the scheduler could
    not start a node and finish one at the same time."""
    merging = await workspace.create_node("merging")
    await work(workspace, merging, files=40)

    results = await asyncio.gather(
        workspace.merge_into_integration("merging"),
        *(workspace.create_node(f"fresh{index}") for index in range(6)),
    )
    merge = results[0]
    assert isinstance(merge, MergeResult)
    assert merge.status is MergeStatus.MERGED
    for node in results[1:]:
        assert isinstance(node, NodeWorktree)
        branch = await git(node.path, "rev-parse", "--abbrev-ref", "HEAD")
        assert branch.strip() == node.branch
    await assert_integration_is_clean(workspace)


async def test_base_ref_stays_immutable_when_a_sibling_merges(
    workspace: SessionWorkspace,
) -> None:
    """``create_node`` pins ``base_ref`` to a commit rather than to the
    integration branch. The comment saying so is now a test: a sibling's merge
    advances the branch, and an earlier node's diff must not notice."""
    first = await workspace.create_node("first")
    second = await workspace.create_node("second")
    assert first.base_ref == second.base_ref
    original_base = first.base_ref

    await work(workspace, first, files=1)
    before = await workspace.diff("first", base_ref=first.base_ref)

    assert (await workspace.merge_into_integration("first")).merged
    await work(workspace, second, files=1)

    # The integration branch moved...
    moved = (
        await git(workspace.integration_path, "rev-parse", workspace.integration_branch)
    ).strip()
    assert moved != original_base
    # ...the pinned refs did not.
    assert first.base_ref == original_base
    assert second.base_ref == original_base
    assert await workspace.diff("first", base_ref=first.base_ref) == before
    diff = await workspace.diff("second", base_ref=second.base_ref)
    assert "second_0.txt" in diff
    assert "first_0.txt" not in diff, "the sibling's merge leaked into the diff"

    # A node created now correctly sees the newer base — pinning is per node, not
    # a frozen session.
    third = await workspace.create_node("third")
    assert third.base_ref == moved


# ---------------------------------------------------------------------------
# C5 — merge serialization
# ---------------------------------------------------------------------------


async def race_into_integration(
    workspace: SessionWorkspace, node_ids: Sequence[str]
) -> list[MergeResult]:
    tasks = [
        asyncio.create_task(workspace.merge_into_integration(node_id), name=node_id)
        for node_id in node_ids
    ]
    return list(await asyncio.gather(*tasks))


@pytest.mark.parametrize("iteration", range(RACE_ITERATIONS))
async def test_racing_merges_all_land_exactly_once(
    repo: Path, tmp_path: Path, iteration: int
) -> None:
    """N nodes finish within microseconds of each other. Every one lands, the
    history holds exactly the commits that reported ``merged``, and no work is
    lost."""
    workspace = await init_session_workspace(
        repo_path=repo,
        session_id=new_session_id(),
        workspaces_root=tmp_path / f"workspaces{iteration}",
    )
    node_ids = [f"n{index}" for index in range(RACE_NODES)]
    nodes = await asyncio.gather(*(workspace.create_node(i) for i in node_ids))
    await asyncio.gather(*(work(workspace, node) for node in nodes))

    results = await race_into_integration(workspace, node_ids)

    assert [r.status for r in results] == [MergeStatus.MERGED] * RACE_NODES
    reported = {r.commit for r in results}
    assert None not in reported
    assert await merge_commits(workspace) == reported
    await assert_integration_is_clean(workspace)
    for node_id in node_ids:
        for index in range(8):
            assert (workspace.integration_path / f"{node_id}_{index}.txt").exists()
        # Reachable from the branch tip, not merely present in the worktree.
        await git(
            workspace.integration_path,
            "cat-file",
            "-e",
            f"{workspace.integration_branch}:{node_id}_0.txt",
        )


@pytest.mark.parametrize("iteration", range(RACE_ITERATIONS))
async def test_racing_merges_with_a_conflict_land_or_block_and_never_raise(
    repo: Path, tmp_path: Path, iteration: int
) -> None:
    """The dangerous mix: some nodes conflict, some do not, all merge at once.

    Unserialized this is where a losing merge reads the *winner's* unmerged index
    and reports a conflict that is not its own — and then aborts the winner's
    merge. Every result must be ``merged`` or ``conflicted``, the conflicts must
    be real, and the worktree must be usable afterwards.
    """
    workspace = await init_session_workspace(
        repo_path=repo,
        session_id=new_session_id(),
        workspaces_root=tmp_path / f"workspaces{iteration}",
    )
    clean_ids = [f"clean{index}" for index in range(3)]
    dirty_ids = [f"dirty{index}" for index in range(3)]

    for node_id in clean_ids:
        await work(workspace, await workspace.create_node(node_id), files=4)
    for node_id in dirty_ids:
        node = await workspace.create_node(node_id)
        (node.path / "a.txt").write_text(f"{node_id}\nline2\n")
        await workspace.commit(node_id, f"feat: {node_id}")

    results = await race_into_integration(workspace, clean_ids + dirty_ids)

    by_id = dict(zip(clean_ids + dirty_ids, results, strict=True))
    for node_id in clean_ids:
        assert by_id[node_id].status is MergeStatus.MERGED, node_id
    # Exactly one of the three nodes editing the same line can win.
    dirty = [by_id[node_id] for node_id in dirty_ids]
    assert sum(r.merged for r in dirty) == 1
    for result in dirty:
        if result.blocked:
            assert result.conflicts == (Path("a.txt"),)

    landed = {r.commit for r in results if r.merged}
    assert await merge_commits(workspace) == landed
    await assert_integration_is_clean(workspace)
    for node_id in clean_ids:
        assert (workspace.integration_path / f"{node_id}_0.txt").exists()

    # The worktree still works: a blocked node's branch can be merged later, once
    # a human has resolved it — which is the whole point of aborting.
    assert (workspace.integration_path / "a.txt").read_text().endswith("line2\n")


async def test_merge_sequences_never_interleave(
    workspace: SessionWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A merge is a *sequence* — is-ancestor, merge, rev-parse, and on conflict
    the abort — and all of it reads one shared index. The lock has to span the
    whole sequence, so no other node may issue a git command in the integration
    worktree in the middle of one."""
    clean_ids = [f"clean{index}" for index in range(2)]
    dirty_ids = [f"dirty{index}" for index in range(2)]
    for node_id in clean_ids:
        await work(workspace, await workspace.create_node(node_id), files=4)
    for node_id in dirty_ids:
        node = await workspace.create_node(node_id)
        (node.path / "a.txt").write_text(f"{node_id}\nline2\n")
        await workspace.commit(node_id, f"feat: {node_id}")

    log: list[GitCall] = []
    spy_on_git(monkeypatch, log)
    results = await race_into_integration(workspace, clean_ids + dirty_ids)

    integration = [call for call in log if call.cwd == workspace.integration_path]
    order = assert_never_overlapped(integration)
    # Each node holds the floor exactly once: its whole sequence is one section,
    # not several interleaved with the others'.
    assert sorted(order) == sorted(clean_ids + dirty_ids)
    # And the abort of a conflicted merge is inside its own section, so the
    # shared worktree is never left MERGING for the next node to trip over.
    aborted = [
        call.owner
        for call in integration
        if call.phase == "start" and call.args[:2] == ("merge", "--abort")
    ]
    assert aborted == [r.source.rsplit("/", 1)[-1] for r in results if r.blocked]


async def test_two_sessions_merge_in_parallel(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock is on the integration worktree, not on the process. A session
    stuck mid-merge must not hold up an unrelated session's merge — with a global
    lock this test deadlocks and times out."""
    first, second = await asyncio.gather(
        init_session_workspace(
            repo_path=repo,
            session_id="sess_first",
            workspaces_root=tmp_path / "workspaces",
        ),
        init_session_workspace(
            repo_path=repo,
            session_id="sess_second",
            workspaces_root=tmp_path / "workspaces",
        ),
    )
    for workspace in (first, second):
        await work(workspace, await workspace.create_node("node"), files=4)

    holding = asyncio.Event()
    release = asyncio.Event()
    original = SessionWorkspace._git

    async def hold_the_first_merge(
        self: SessionWorkspace, cwd: Path, *args: str, **kwargs: Any
    ) -> Any:
        if args[0] == "merge" and self.session_id == first.session_id:
            holding.set()
            await release.wait()
        return await original(self, cwd, *args, **kwargs)

    monkeypatch.setattr(SessionWorkspace, "_git", hold_the_first_merge)

    stuck = asyncio.create_task(first.merge_into_integration("node"))
    try:
        await asyncio.wait_for(holding.wait(), timeout=10)
        # The first session is frozen mid-merge. If the mutex were global rather
        # than per integration worktree, this line would never return.
        unrelated = await asyncio.wait_for(
            second.merge_into_integration("node"), timeout=10
        )
    finally:
        release.set()

    assert unrelated.status is MergeStatus.MERGED
    assert (await stuck).status is MergeStatus.MERGED
    assert await first.integration_lock() is not await second.integration_lock()


async def test_rebuilt_workspace_values_share_one_merge_lock(
    workspace: SessionWorkspace, tmp_path: Path
) -> None:
    """`SessionWorkspace` is a value object that callers rebuild from database
    columns on every operation (`SingleRunService._workspace`). The lock is keyed
    by the resolved integration path precisely so those rebuilds — and any other
    spelling of the same directory — still serialize against each other."""

    def rebuilt(root: Path) -> SessionWorkspace:
        return SessionWorkspace(
            session_id=workspace.session_id,
            repo_path=workspace.repo_path,
            root=root,
        )

    link = tmp_path / "linked-workspace"
    link.symlink_to(workspace.root, target_is_directory=True)

    lock = await workspace.integration_lock()
    assert await rebuilt(workspace.root).integration_lock() is lock
    assert await rebuilt(link).integration_lock() is lock

    node_ids = [f"n{index}" for index in range(4)]
    nodes = await asyncio.gather(*(workspace.create_node(i) for i in node_ids))
    await asyncio.gather(*(work(workspace, node, files=4) for node in nodes))

    merges: list[Callable[[], Coroutine[Any, Any, MergeResult]]] = [
        (
            lambda node_id=node_id, root=root: rebuilt(root).merge_into_integration(
                node_id
            )
        )  # type: ignore[misc]
        for node_id, root in zip(
            node_ids, [workspace.root, link, workspace.root, link], strict=True
        )
    ]
    results = await asyncio.gather(*(call() for call in merges))

    assert [r.status for r in results] == [MergeStatus.MERGED] * len(node_ids)
    assert await merge_commits(workspace) == {r.commit for r in results}
    await assert_integration_is_clean(workspace)
