"""Git worktree lifecycle for graph nodes (`design.md` §2.2, invariant 2).

This is the **only** module in the codebase that shells out to ``git``
(`docs/architecture.md` §1 rule 4). Everything else asks it for a path.

Layout it manages::

    <workspaces_root>/
      sess_01H.../
        integration/    -> branch agenthub/sess_01H/integration
        node_a/         -> worktree, branch agenthub/sess_01H/node_a

Two deviations from `design.md` §2.2 that real git forces:

1. The integration branch cannot be ``agenthub/<sess>`` while node branches are
   ``agenthub/<sess>/<node>``. Git stores refs as files, so
   ``refs/heads/agenthub/sess1`` (a file) makes ``refs/heads/agenthub/sess1/node_a``
   (which needs a directory of that name) impossible: *"cannot lock ref ...
   'refs/heads/agenthub/sess1' exists"*. The integration branch is therefore
   ``agenthub/<sess>/integration`` and ``integration`` is a reserved node id.
2. ``git worktree add <path> -b <branch> <base_ref>`` takes exactly one
   commit-ish. "The merge of the parent nodes' branches" is not a ref, so a node
   with several parents is created off its first parent and the remaining parents
   are folded in with ``git merge`` inside the fresh worktree
   (see :meth:`SessionWorkspace.create_node`).

Conventions that are load-bearing here:

* Every git invocation goes through :func:`_git` /
  ``asyncio.create_subprocess_exec``. No ``subprocess.run`` — a synchronous git
  call stalls the PTY stream of every other node (invariant 5).
* A merge conflict is a **return value** (:class:`MergeResult` with
  ``status=CONFLICTED``), never an exception; it becomes the node's ``blocked``
  state (`docs/architecture.md` §9). Exceptions are reserved for bugs: an unsafe
  node id, a path that escapes the session workspace, a directory that is not a
  repository, git missing.
"""

import asyncio
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

import structlog

from app.models.clock import now_ms
from app.models.ids import NodeId, SessionId

log = structlog.get_logger()

BRANCH_NAMESPACE = "agenthub"
INTEGRATION = "integration"

# `integration` is a directory and a branch we own; a node may not claim it.
RESERVED_NODE_IDS = frozenset({INTEGRATION})

# Node and session ids are ULID-prefixed (`app.models.ids`), so this is generous.
# What it must exclude is anything that could escape the session workspace or be
# read by git as an option: `/`, `..`, a leading `-`.
_SAFE_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")

# Commits this module makes must work on a machine with no global git identity,
# so the identity is passed per invocation with `-c`. Agent work is attributed to
# the orchestrator rather than to the human: `git log --author=AgentHub` then
# separates generated commits from hand-written ones, and `.local` is a reserved
# TLD that can never resolve to a real mailbox.
AGENT_IDENTITY_NAME = "AgentHub Agent"
AGENT_IDENTITY_EMAIL = "agent@agenthub.local"


@dataclass(frozen=True, slots=True)
class GitIdentity:
    name: str
    email: str


AGENT_IDENTITY = GitIdentity(AGENT_IDENTITY_NAME, AGENT_IDENTITY_EMAIL)


# TODO(phase-1): reparent onto the shared `AgentHubError` from
# `docs/conventions.md` §2 once `app/errors.py` exists.
class WorktreeError(Exception):
    """A bug: bad input, a broken repository, or git behaving unexpectedly."""


class InvalidNameError(WorktreeError):
    """A session or node id that cannot safely become a path or a branch."""


class PathEscapeError(WorktreeError):
    """A path that would land outside the session workspace (conventions §6)."""


class NotARepositoryError(WorktreeError):
    """The target path is not a usable git repository."""


class GitCommandError(WorktreeError):
    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            f"git failed (exit {returncode}): {' '.join(self.argv)}\n{stderr.strip()}"
        )


class MergeStatus(StrEnum):
    MERGED = "merged"
    NOTHING_TO_MERGE = "nothing_to_merge"
    CONFLICTED = "conflicted"


class CommitStatus(StrEnum):
    COMMITTED = "committed"
    NOTHING_TO_COMMIT = "nothing_to_commit"


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Outcome of folding one branch into another.

    ``CONFLICTED`` is a normal result, not a failure of this module: the merge is
    aborted so the worktree stays usable, ``conflicts`` lists the repo-relative
    paths git could not reconcile, and the caller moves the node to ``blocked``.
    """

    status: MergeStatus
    source: str
    target: str
    conflicts: tuple[Path, ...] = ()
    commit: str | None = None
    ts: int = field(default_factory=now_ms)

    @property
    def merged(self) -> bool:
        return self.status is MergeStatus.MERGED

    @property
    def blocked(self) -> bool:
        return self.status is MergeStatus.CONFLICTED


@dataclass(frozen=True, slots=True)
class CommitResult:
    """Outcome of committing a node's work.

    An agent that changed no files is a real result, so ``NOTHING_TO_COMMIT`` is
    an outcome, not an error.
    """

    status: CommitStatus
    branch: str
    commit: str | None = None
    changed_paths: tuple[Path, ...] = ()
    ts: int = field(default_factory=now_ms)

    @property
    def committed(self) -> bool:
        return self.status is CommitStatus.COMMITTED


@dataclass(frozen=True, slots=True)
class NodeWorktree:
    """A created node worktree.

    ``parent_merges`` is empty for a node with zero or one parent. With several
    parents it holds the result of folding parents 2..n onto the first; if any of
    them conflicted the worktree exists but the node is already ``blocked`` and
    must not be handed to a harness.
    """

    node_id: NodeId
    path: Path
    branch: str
    base_ref: str
    parent_merges: tuple[MergeResult, ...] = ()

    @property
    def blocked(self) -> bool:
        return any(m.blocked for m in self.parent_merges)

    @property
    def conflicts(self) -> tuple[Path, ...]:
        return tuple(p for m in self.parent_merges for p in m.conflicts)


def default_workspaces_root() -> Path:
    return Path.home() / ".agenthub" / "workspaces"


async def _real_path(path: Path) -> Path:
    """``expanduser().resolve()`` off the event loop.

    Resolution stats every component, and on macOS it matters for correctness
    too: ``/var`` is a symlink to ``/private/var``, so an unresolved path never
    compares equal to what ``git worktree list`` prints.
    """
    return await asyncio.to_thread(lambda: path.expanduser().resolve())


@dataclass(frozen=True, slots=True)
class SessionWorkspace:
    """The worktrees of one session. Build it with :func:`init_session_workspace`.

    ``repo_path`` is the user's repository. It is used for exactly one command —
    ``git worktree add`` for the integration worktree, which does not touch its
    working tree — and never as an agent's cwd (invariant 2). Every other command
    runs inside a worktree this class owns.
    """

    session_id: SessionId
    repo_path: Path
    root: Path
    identity: GitIdentity = AGENT_IDENTITY

    @property
    def integration_branch(self) -> str:
        return f"{BRANCH_NAMESPACE}/{self.session_id}/{INTEGRATION}"

    @property
    def integration_path(self) -> Path:
        return self.root / INTEGRATION

    def node_branch(self, node_id: NodeId) -> str:
        return f"{BRANCH_NAMESPACE}/{self.session_id}/{_valid_node_id(node_id)}"

    def node_path(self, node_id: NodeId) -> Path:
        candidate = self.root / _valid_node_id(node_id)
        # Belt and braces: the regex already forbids separators and `..`, but
        # containment is the invariant we actually care about (conventions §6).
        if candidate.parent != self.root or ".." in candidate.parts:
            raise PathEscapeError(
                f"node path {candidate} escapes session workspace {self.root}"
            )
        return candidate

    async def create_node(
        self,
        node_id: NodeId,
        *,
        parents: Sequence[NodeId] = (),
    ) -> NodeWorktree:
        """``git worktree add`` a fresh worktree for one node.

        The base is the integration branch when there are no parents, otherwise
        the first parent's branch with the remaining parents merged in.
        """
        path = self.node_path(node_id)
        branch = self.node_branch(node_id)
        parent_branches = [self.node_branch(p) for p in parents]
        source_ref = parent_branches[0] if parent_branches else self.integration_branch

        await self._git(
            self.integration_path,
            "worktree",
            "add",
            "-b",
            branch,
            "--",
            str(path),
            source_ref,
        )

        merges: list[MergeResult] = []
        for extra in parent_branches[1:]:
            result = await self._merge(path, source=extra, target=branch)
            merges.append(result)
            if result.blocked:
                # Stop folding: the base is already unbuildable and every further
                # merge would report a conflict the human has not been shown yet.
                break

        # Persist an immutable commit, not the symbolic integration branch. The
        # latter advances when this node merges and would make its final diff
        # appear empty after a restart.
        base_ref = await self._rev_parse(path, "HEAD")
        worktree = NodeWorktree(
            node_id=node_id,
            path=path,
            branch=branch,
            base_ref=base_ref,
            parent_merges=tuple(merges),
        )
        log.info(
            "worktree.created",
            session_id=self.session_id,
            node_id=node_id,
            branch=branch,
            base_ref=base_ref,
            blocked=worktree.blocked,
        )
        return worktree

    async def diff(self, node_id: NodeId, *, base_ref: str) -> str:
        """The node's patch from its immutable creation checkpoint."""
        path = self.node_path(node_id)
        base = await self._rev_parse(path, f"{base_ref}^{{commit}}")
        branch = self.node_branch(node_id)
        result = await self._git(
            path,
            "diff",
            "--no-ext-diff",
            "--binary",
            "--find-renames",
            "--end-of-options",
            base,
            branch,
            "--",
        )
        return result.stdout

    async def commit(self, node_id: NodeId, message: str) -> CommitResult:
        """Stage everything in the node worktree and commit it."""
        path = self.node_path(node_id)
        branch = self.node_branch(node_id)

        await self._git(path, "add", "--all", "--")
        staged = await self._git(path, "diff", "--cached", "--name-only", "-z", "--")
        changed = _split_nul_paths(staged.stdout)
        if not changed:
            log.info(
                "worktree.nothing_to_commit",
                session_id=self.session_id,
                node_id=node_id,
            )
            return CommitResult(CommitStatus.NOTHING_TO_COMMIT, branch)

        # --no-verify: the target repo's hooks are the human's policy, applied at
        # review time on the integration branch, not to every agent checkpoint.
        await self._git(path, "commit", "--no-verify", "-m", message, identity=True)
        head = await self._rev_parse(path, "HEAD")
        log.info(
            "worktree.committed",
            session_id=self.session_id,
            node_id=node_id,
            commit=head,
            files=len(changed),
        )
        return CommitResult(
            CommitStatus.COMMITTED,
            branch,
            commit=head,
            changed_paths=changed,
        )

    async def merge_into_integration(
        self,
        node_id: NodeId,
        *,
        message: str | None = None,
    ) -> MergeResult:
        """Merge a node branch into the integration branch.

        Returns ``CONFLICTED`` (never raises) when git cannot reconcile the
        change; the merge is aborted first so the shared integration worktree
        does not stay half-merged and block every other node.
        """
        branch = self.node_branch(node_id)
        return await self._merge(
            self.integration_path,
            source=branch,
            target=self.integration_branch,
            message=message or f"agenthub: merge {node_id} into {INTEGRATION}",
        )

    async def remove_node(self, node_id: NodeId, *, force: bool = False) -> None:
        """Remove the node's worktree. Idempotent.

        The branch is kept on purpose: it is the node's diff, its rollback point
        and the parent of whatever integration merged. Garbage-collecting
        branches is a session-level decision, not a node-level one.
        """
        path = self.node_path(node_id)
        if await _real_path(path) not in await self.list_worktrees():
            await self._git(self.integration_path, "worktree", "prune")
            return

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args += ["--", str(path)]
        await self._git(self.integration_path, *args)
        await self._git(self.integration_path, "worktree", "prune")
        log.info("worktree.removed", session_id=self.session_id, node_id=node_id)

    async def list_worktrees(self) -> tuple[Path, ...]:
        """Resolved paths of every worktree of the repository, including the
        target repo itself and the integration worktree."""
        result = await self._git(
            self.integration_path, "worktree", "list", "--porcelain"
        )
        return _parse_worktree_list(result.stdout)

    async def _merge(
        self,
        worktree: Path,
        *,
        source: str,
        target: str,
        message: str | None = None,
    ) -> MergeResult:
        if await self._is_ancestor(worktree, source, "HEAD"):
            return MergeResult(MergeStatus.NOTHING_TO_MERGE, source, target)

        result = await self._git(
            worktree,
            "merge",
            "--no-ff",
            "--no-edit",
            "-m",
            message or f"agenthub: merge {source} into {target}",
            "--",
            source,
            identity=True,
            ok_codes=(0, 1),
        )
        if result.returncode == 0:
            head = await self._rev_parse(worktree, "HEAD")
            log.info("worktree.merged", source=source, target=target, commit=head)
            return MergeResult(MergeStatus.MERGED, source, target, commit=head)

        conflicts = await self._conflicted_paths(worktree)
        if not conflicts:
            # Exit 1 with nothing unmerged is not a conflict — a dirty worktree,
            # a mid-merge state, an unrelated history. That is a bug upstream.
            raise GitCommandError(result.argv, result.returncode, result.stderr)

        await self._git(worktree, "merge", "--abort")
        log.warning(
            "worktree.merge_conflict",
            source=source,
            target=target,
            conflicts=[str(p) for p in conflicts],
        )
        return MergeResult(MergeStatus.CONFLICTED, source, target, conflicts=conflicts)

    async def _conflicted_paths(self, worktree: Path) -> tuple[Path, ...]:
        result = await self._git(
            worktree, "diff", "--name-only", "--diff-filter=U", "-z", "--"
        )
        return _split_nul_paths(result.stdout)

    async def _is_ancestor(self, worktree: Path, ref: str, of: str) -> bool:
        result = await self._git(
            worktree, "merge-base", "--is-ancestor", ref, of, ok_codes=(0, 1)
        )
        return result.returncode == 0

    async def _rev_parse(self, worktree: Path, ref: str) -> str:
        result = await self._git(
            worktree, "rev-parse", "--verify", "--end-of-options", ref
        )
        return result.stdout.strip()

    async def _git(
        self,
        cwd: Path,
        *args: str,
        identity: bool = False,
        ok_codes: tuple[int, ...] = (0,),
    ) -> "_GitResult":
        return await _git(
            cwd,
            *args,
            identity=self.identity if identity else None,
            ok_codes=ok_codes,
        )


async def init_session_workspace(
    *,
    repo_path: Path,
    session_id: SessionId,
    workspaces_root: Path | None = None,
    base_ref: str = "HEAD",
    identity: GitIdentity = AGENT_IDENTITY,
) -> SessionWorkspace:
    """Create ``<workspaces_root>/<session_id>/integration`` and its branch.

    Idempotent: if the integration worktree is already registered (orchestrator
    restart) the existing workspace is returned untouched.
    """
    _valid_name(session_id, kind="session id")
    repo = await _real_path(repo_path)

    probe = await _git(repo, "rev-parse", "--git-common-dir", ok_codes=(0, 128))
    if probe.returncode != 0:
        raise NotARepositoryError(f"{repo} is not a git repository: {probe.stderr}")

    # Pin the base to a sha now: it cannot be re-read as an option, and the
    # session is not silently rebased if the user moves the branch afterwards.
    resolved_base = await _git(
        repo,
        "rev-parse",
        "--verify",
        "--end-of-options",
        f"{base_ref}^{{commit}}",
        ok_codes=(0, 128),
    )
    if resolved_base.returncode != 0:
        raise NotARepositoryError(
            f"{repo} has no commit for base ref {base_ref!r}: {resolved_base.stderr}"
        )
    base_commit = resolved_base.stdout.strip()

    root = (workspaces_root or default_workspaces_root()) / session_id
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    workspace = SessionWorkspace(
        session_id=session_id,
        repo_path=repo,
        root=await _real_path(root),
        identity=identity,
    )

    existing = await _git(repo, "worktree", "list", "--porcelain")
    integration = await _real_path(workspace.integration_path)
    if integration in _parse_worktree_list(existing.stdout):
        return workspace

    await _git(
        repo,
        "worktree",
        "add",
        "-b",
        workspace.integration_branch,
        "--",
        str(workspace.integration_path),
        base_commit,
    )
    log.info(
        "worktree.session_initialized",
        session_id=session_id,
        repo=str(repo),
        branch=workspace.integration_branch,
        base=base_commit,
    )
    return workspace


@dataclass(frozen=True, slots=True)
class _GitResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


_GIT_ENV = {
    "GIT_TERMINAL_PROMPT": "0",  # never let a credential prompt hang a node
    "LC_ALL": "C",  # stable messages, whatever the user's locale is
}


async def _git(
    cwd: Path,
    *args: str,
    identity: GitIdentity | None = None,
    ok_codes: tuple[int, ...] = (0,),
) -> _GitResult:
    argv = ["git", "-C", str(cwd)]
    if identity is not None:
        argv += [
            "-c",
            f"user.name={identity.name}",
            "-c",
            f"user.email={identity.email}",
            # A developer's global `commit.gpgsign=true` must not make agent
            # commits depend on an unlocked key.
            "-c",
            "commit.gpgsign=false",
        ]
    argv += args

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **_GIT_ENV},
        )
    except FileNotFoundError as err:  # git itself is missing
        raise WorktreeError("git executable not found on PATH") from err

    stdout, stderr = await proc.communicate()
    result = _GitResult(
        argv=tuple(argv),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )
    if result.returncode not in ok_codes:
        raise GitCommandError(result.argv, result.returncode, result.stderr)
    return result


def _valid_name(value: str, *, kind: str) -> str:
    if not _SAFE_NAME.match(value) or ".." in value or value.endswith((".", ".lock")):
        raise InvalidNameError(f"unsafe {kind}: {value!r}")
    return value


def _valid_node_id(node_id: NodeId) -> NodeId:
    if node_id in RESERVED_NODE_IDS:
        raise InvalidNameError(f"node id {node_id!r} is reserved")
    return _valid_name(node_id, kind="node id")


def _split_nul_paths(payload: str) -> tuple[Path, ...]:
    return tuple(Path(part) for part in payload.split("\0") if part)


def _parse_worktree_list(porcelain: str) -> tuple[Path, ...]:
    return tuple(
        Path(line.removeprefix("worktree "))
        for line in porcelain.splitlines()
        if line.startswith("worktree ")
    )
