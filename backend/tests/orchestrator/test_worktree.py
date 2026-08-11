"""Integration tests for the worktree lifecycle against a real temp repository.

Mocking git here would test our assumptions instead of git's behavior, and the
whole risk of this module is that the two differ (`docs/architecture.md` §10).
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from app.models.ids import new_session_id
from app.orchestrator.worktree import (
    CommitStatus,
    GitCommandError,
    InvalidNameError,
    MergeStatus,
    NotARepositoryError,
    SessionWorkspace,
    WorktreeError,
    default_workspaces_root,
    init_session_workspace,
)


async def git(cwd: Path, *args: str) -> str:
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
    output = stdout.decode()
    assert proc.returncode == 0, f"git {args} failed:\n{output}"
    return output


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


async def test_init_creates_integration_worktree(
    workspace: SessionWorkspace, repo: Path
) -> None:
    assert workspace.integration_path.is_dir()
    assert (workspace.integration_path / "a.txt").read_text() == "line1\nline2\n"

    branch = await git(workspace.integration_path, "rev-parse", "--abbrev-ref", "HEAD")
    assert branch.strip() == workspace.integration_branch
    assert workspace.integration_branch.endswith("/integration")

    # The user's repository keeps its own branch checked out.
    assert (await git(repo, "rev-parse", "--abbrev-ref", "HEAD")).strip() == "main"


async def test_init_is_idempotent(repo: Path, tmp_path: Path) -> None:
    session_id = new_session_id()
    first = await init_session_workspace(
        repo_path=repo,
        session_id=session_id,
        workspaces_root=tmp_path / "workspaces",
    )
    second = await init_session_workspace(
        repo_path=repo,
        session_id=session_id,
        workspaces_root=tmp_path / "workspaces",
    )
    assert first == second
    listed = await second.list_worktrees()
    assert listed.count(second.integration_path.resolve()) == 1


async def test_init_rejects_a_directory_that_is_not_a_repository(
    tmp_path: Path,
) -> None:
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(NotARepositoryError):
        await init_session_workspace(
            repo_path=plain,
            session_id=new_session_id(),
            workspaces_root=tmp_path / "workspaces",
        )


async def test_happy_path_create_commit_merge(workspace: SessionWorkspace) -> None:
    node = await workspace.create_node("node_a")

    assert node.path.is_dir()
    assert node.branch.endswith("/node_a")
    assert (
        node.base_ref
        == (await git(workspace.integration_path, "rev-parse", "HEAD")).strip()
    )
    assert not node.blocked

    (node.path / "greeting.txt").write_text("hello from the agent\n")
    commit = await workspace.commit("node_a", "feat: greeting")

    assert commit.status is CommitStatus.COMMITTED
    assert commit.committed
    assert commit.commit is not None
    assert commit.changed_paths == (Path("greeting.txt"),)

    merge = await workspace.merge_into_integration("node_a")

    assert merge.status is MergeStatus.MERGED
    assert merge.conflicts == ()
    assert merge.commit is not None
    # The agent's file is on the integration branch, not just in its worktree.
    assert (workspace.integration_path / "greeting.txt").read_text() == (
        "hello from the agent\n"
    )
    blob = await git(
        workspace.integration_path,
        "show",
        f"{workspace.integration_branch}:greeting.txt",
    )
    assert blob == "hello from the agent\n"


async def test_agent_commits_do_not_need_a_global_git_identity(
    workspace: SessionWorkspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clean machine: no ~/.gitconfig, no /etc/gitconfig, so no user.name.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")

    node = await workspace.create_node("node_a")
    (node.path / "greeting.txt").write_text("hi\n")
    await workspace.commit("node_a", "feat: greeting")

    author = await git(node.path, "log", "-1", "--format=%an <%ae>")
    assert author.strip() == "AgentHub Agent <agent@agenthub.local>"


async def test_commit_without_changes_is_not_an_error(
    workspace: SessionWorkspace,
) -> None:
    node = await workspace.create_node("node_a")

    result = await workspace.commit(
        "node_a", "feat: nothing happened", base_ref=node.base_ref
    )

    assert result.status is CommitStatus.NOTHING_TO_COMMIT
    assert not result.committed
    assert result.commit is None
    assert result.changed_paths == ()


async def test_agent_authored_commit_is_a_valid_checkpoint(
    workspace: SessionWorkspace,
) -> None:
    node = await workspace.create_node("node_a")
    (node.path / "products.html").write_text("kiwi products\n")
    await git(node.path, "add", "--all", "--")
    await git(node.path, "commit", "-m", "feat: add products page")

    result = await workspace.commit(
        "node_a", "agent: add products page", base_ref=node.base_ref
    )

    assert result.status is CommitStatus.CHECKPOINTED
    assert result.committed
    assert result.commit == (await git(node.path, "rev-parse", "HEAD")).strip()
    assert result.changed_paths == (Path("products.html"),)


async def test_conflicting_merge_returns_blocked_and_does_not_raise(
    workspace: SessionWorkspace,
) -> None:
    node_a = await workspace.create_node("node_a")
    node_b = await workspace.create_node("node_b")

    (node_a.path / "a.txt").write_text("from A\nline2\n")
    await workspace.commit("node_a", "feat: a")
    (node_b.path / "a.txt").write_text("from B\nline2\n")
    await workspace.commit("node_b", "feat: b")

    first = await workspace.merge_into_integration("node_a")
    assert first.status is MergeStatus.MERGED

    second = await workspace.merge_into_integration("node_b")

    assert second.status is MergeStatus.CONFLICTED
    assert second.blocked
    assert second.conflicts == (Path("a.txt"),)
    assert second.source == node_b.branch
    assert second.target == workspace.integration_branch

    # The merge was aborted: the shared integration worktree is clean and the
    # next node can still merge, instead of every node inheriting the conflict.
    status = await git(workspace.integration_path, "status", "--porcelain")
    assert status == ""
    assert (workspace.integration_path / "a.txt").read_text() == "from A\nline2\n"


async def test_merging_the_same_node_twice_reports_nothing_to_merge(
    workspace: SessionWorkspace,
) -> None:
    node = await workspace.create_node("node_a")
    (node.path / "greeting.txt").write_text("hello\n")
    await workspace.commit("node_a", "feat: greeting")

    assert (await workspace.merge_into_integration("node_a")).merged
    again = await workspace.merge_into_integration("node_a")

    assert again.status is MergeStatus.NOTHING_TO_MERGE
    assert again.commit is None


async def test_merging_a_node_that_did_nothing_reports_nothing_to_merge(
    workspace: SessionWorkspace,
) -> None:
    await workspace.create_node("node_a")

    result = await workspace.merge_into_integration("node_a")

    assert result.status is MergeStatus.NOTHING_TO_MERGE


async def test_node_with_two_parents_is_based_on_both(
    workspace: SessionWorkspace,
) -> None:
    parent_a = await workspace.create_node("node_a")
    (parent_a.path / "a-work.txt").write_text("a\n")
    await workspace.commit("node_a", "feat: a")

    parent_b = await workspace.create_node("node_b")
    (parent_b.path / "b-work.txt").write_text("b\n")
    await workspace.commit("node_b", "feat: b")

    child = await workspace.create_node("node_c", parents=["node_a", "node_b"])

    assert not child.blocked
    assert child.base_ref == (await git(child.path, "rev-parse", "HEAD")).strip()
    assert [m.status for m in child.parent_merges] == [MergeStatus.MERGED]
    assert (child.path / "a-work.txt").exists()
    assert (child.path / "b-work.txt").exists()


async def test_node_with_conflicting_parents_is_blocked(
    workspace: SessionWorkspace,
) -> None:
    parent_a = await workspace.create_node("node_a")
    (parent_a.path / "a.txt").write_text("from A\nline2\n")
    await workspace.commit("node_a", "feat: a")

    parent_b = await workspace.create_node("node_b")
    (parent_b.path / "a.txt").write_text("from B\nline2\n")
    await workspace.commit("node_b", "feat: b")

    child = await workspace.create_node("node_c", parents=["node_a", "node_b"])

    assert child.blocked
    assert child.conflicts == (Path("a.txt"),)
    status = await git(child.path, "status", "--porcelain")
    assert status == ""


async def test_remove_node_deletes_the_worktree(workspace: SessionWorkspace) -> None:
    node = await workspace.create_node("node_a")
    assert node.path.resolve() in await workspace.list_worktrees()

    await workspace.remove_node("node_a")

    assert not node.path.exists()
    assert node.path.resolve() not in await workspace.list_worktrees()
    # The branch survives removal: it is the node's diff and rollback point.
    refs = await git(workspace.integration_path, "branch", "--list", node.branch)
    assert node.branch in refs


async def test_remove_node_is_idempotent(workspace: SessionWorkspace) -> None:
    await workspace.create_node("node_a")
    await workspace.remove_node("node_a")
    await workspace.remove_node("node_a")


async def test_remove_node_refuses_to_drop_uncommitted_work_unless_forced(
    workspace: SessionWorkspace,
) -> None:
    node = await workspace.create_node("node_a")
    (node.path / "scratch.txt").write_text("unsaved\n")

    with pytest.raises(GitCommandError):
        await workspace.remove_node("node_a")

    await workspace.remove_node("node_a", force=True)
    assert node.path.resolve() not in await workspace.list_worktrees()


@pytest.mark.parametrize(
    "node_id",
    ["../../evil", "..", "/etc/passwd", "node_a/../../escape", "-force", ".hidden", ""],
)
async def test_unsafe_node_ids_are_rejected(
    workspace: SessionWorkspace, node_id: str
) -> None:
    with pytest.raises(InvalidNameError):
        workspace.node_path(node_id)
    with pytest.raises(InvalidNameError):
        await workspace.create_node(node_id)


async def test_a_node_may_not_be_called_integration(
    workspace: SessionWorkspace,
) -> None:
    with pytest.raises(InvalidNameError):
        await workspace.create_node("integration")


async def test_escaping_node_ids_never_touch_the_target_repository(
    workspace: SessionWorkspace, repo: Path
) -> None:
    with pytest.raises(InvalidNameError):
        await workspace.create_node("../../target-repo")

    assert (repo / "a.txt").read_text() == "line1\nline2\n"
    assert (await git(repo, "status", "--porcelain")) == ""


def test_default_workspaces_root_is_under_agenthub() -> None:
    assert default_workspaces_root().parts[-2:] == (".agenthub", "workspaces")


async def test_diff_survives_integration_merge(workspace: SessionWorkspace) -> None:
    node = await workspace.create_node("node_a")
    (node.path / "greeting.txt").write_text("hello\n")
    await workspace.commit("node_a", "feat: greeting")

    before = await workspace.diff("node_a", base_ref=node.base_ref)
    await workspace.merge_into_integration("node_a")
    after = await workspace.diff("node_a", base_ref=node.base_ref)

    assert before == after
    assert "greeting.txt" in after
    assert "+hello" in after


async def test_integration_diff_contains_the_aggregate_result(
    workspace: SessionWorkspace,
) -> None:
    first = await workspace.create_node("node_a")
    (first.path / "header.txt").write_text("kiwi header\n")
    await workspace.commit("node_a", "feat: header")
    await workspace.merge_into_integration("node_a")

    second = await workspace.create_node("node_b")
    (second.path / "products.txt").write_text("kiwi products\n")
    await workspace.commit("node_b", "feat: products")
    await workspace.merge_into_integration("node_b")

    patch = await workspace.integration_diff(
        base_refs=(first.base_ref, second.base_ref)
    )

    assert "header.txt" in patch
    assert "+kiwi header" in patch
    assert "products.txt" in patch
    assert "+kiwi products" in patch


async def test_finalize_keeps_one_result_branch_and_removes_worktrees(
    workspace: SessionWorkspace,
    repo: Path,
) -> None:
    node = await workspace.create_node("node_a")
    (node.path / "result.txt").write_text("complete result\n")
    await workspace.commit("node_a", "feat: result")
    await workspace.merge_into_integration("node_a")
    expected = (await git(workspace.integration_path, "rev-parse", "HEAD")).strip()

    result = await workspace.finalize(node_ids=("node_a",))

    assert result.branch == workspace.result_branch
    assert result.commit == expected
    assert not node.path.exists()
    assert not workspace.integration_path.exists()
    assert (await git(repo, "rev-parse", workspace.result_branch)).strip() == expected
    assert await git(repo, "show", f"{workspace.result_branch}:result.txt") == (
        "complete result\n"
    )
    assert await git(repo, "branch", "--list", workspace.integration_branch) == ""
    assert node.branch in await git(repo, "branch", "--list", node.branch)
    assert (await git(repo, "rev-parse", "HEAD")).strip() != expected

    repeated = await workspace.finalize(node_ids=("node_a",))
    assert repeated.commit == expected
    assert repeated.removed_worktrees == ()


async def test_finalize_preserves_every_worktree_when_one_is_dirty(
    workspace: SessionWorkspace,
) -> None:
    node = await workspace.create_node("node_a")
    (node.path / "result.txt").write_text("committed\n")
    await workspace.commit("node_a", "feat: result")
    await workspace.merge_into_integration("node_a")
    (node.path / "scratch.txt").write_text("not committed\n")

    with pytest.raises(WorktreeError, match="dirty worktree"):
        await workspace.finalize(node_ids=("node_a",))

    assert node.path.exists()
    assert workspace.integration_path.exists()
