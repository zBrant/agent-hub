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

Concurrency, measured rather than assumed (C4/C5). Two separate mutexes, because
the two races have different scopes — see :class:`_LockRegistry`:

* **Registering worktrees in parallel is not safe**, though it looks like it is.
  ``git worktree add`` creates ``.git/worktrees/<id>/`` and fills in ``commondir``
  in two steps, and a *concurrent* add enumerates that same directory: catching
  the file at zero bytes kills it with ``fatal: failed to read
  .git/worktrees/<other>/commondir: Undefined error: 0`` (exit 128, errno unset —
  a short read, not a missing file). Measured on git 2.39.5: 0 failures in 60
  rounds at 2, 3, 4 and 8-way concurrency, 3 in 60 at 16-way, 4 in 60 at 24-way.
  Rare at the scheduler's ``max_concurrency`` of 2-3, and a hard exception at
  exactly the wrong moment — while a node is being materialized. So worktree
  *registration* is serialized per repository: only ``add``/``remove``/``prune``
  are inside the mutex, so a node is never waiting on another node's agent, and
  the checkout is short next to the run that follows. ``worktree list`` survived
  a thousand concurrent attempts — it tolerates a half-written entry — and stays
  unlocked, which also keeps ``remove_node`` from deadlocking on itself, since
  ``asyncio.Lock`` is not reentrant.
* **Merging in parallel is not safe**, and git does not fail cleanly. Its locking
  is per file (``index.lock``, ``<ref>.lock``) and is released between the
  commands of a merge *sequence*, so a second ``git merge`` started while the
  first is unfinished produces, depending on timing: exit 128 *"You have not
  concluded your merge (MERGE_HEAD exists)"*, exit 128 *"stash failed"*, exit 128
  *"Unable to create index.lock"*, or exit **1** *"Unable to write index"* — the
  last of which is the dangerous one, since exit 1 is also how a real conflict
  exits. Worse, whichever loser reaches ``git merge --abort`` first aborts the
  *winner's* merge. Measured with five nodes: one commit landed and four raised;
  in one run of five, nothing landed at all and the shared worktree was left
  dirty. Hence :meth:`SessionWorkspace.merge_into_integration` holds a mutex for
  the whole sequence, abort included.

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


class InvalidBranchNameError(WorktreeError):
    """A user-authored final branch is not a valid local Git branch name."""


class BranchAlreadyExistsError(WorktreeError):
    """A final branch would overwrite or conflict with an existing local ref."""


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
    CHECKPOINTED = "checkpointed"
    """The agent committed its own work before the orchestrator checkpoint."""

    NOTHING_TO_COMMIT = "nothing_to_commit"


@dataclass(frozen=True, slots=True)
class FinalizeResult:
    """A durable result ref left after temporary worktrees are removed."""

    branch: str
    commit: str
    removed_worktrees: tuple[Path, ...] = ()
    removed_branches: tuple[str, ...] = ()


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
        return self.status in (CommitStatus.COMMITTED, CommitStatus.CHECKPOINTED)


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


class _LockRegistry:
    """Mutexes for a shared *thing on disk*, keyed by its resolved path.

    Why not an attribute of :class:`SessionWorkspace`: the workspace is a frozen
    value object that callers rebuild from database columns whenever they need it
    (``NodeRunService._workspace``), so a per-instance lock would be a brand
    new, uncontended lock on every call and would serialize nothing at all. What
    is being protected is the directory, not the Python object describing it, so
    the lock's identity has to come from the path. Resolved, because ``/var`` and
    ``/private/var`` are the same worktree and must not get two mutexes.

    Why not one global lock: the two registries below have deliberately different
    scopes. Merges contend per *integration worktree* — two sessions have two of
    them and must never wait on each other. Worktree registration contends per
    *repository*, because ``.git/worktrees/`` is shared by every session opened
    on that repository.

    In-process only, deliberately. A cross-process guard (an ``flock`` on a file
    beside the repository) would be needed if two ``agenthub`` processes could
    drive one workspace, and git would not provide it: ``git worktree lock`` only
    marks a worktree as not-prunable, and ``index.lock`` covers a single command,
    not a merge *sequence*. But AgentHub is a single-user tool bound to
    127.0.0.1 with one orchestrator process (`design.md` §1), and the SQLite
    projection and the run event log already assume a single writer — a git lock
    alone would advertise a guarantee the rest of the process does not keep. So
    it is out of scope, along with the human who runs ``git merge`` by hand in
    the integration worktree. Both fail loudly rather than silently; the failure
    modes are in the module docstring.
    """

    def __init__(self, purpose: str) -> None:
        self._purpose = purpose
        self._locks: dict[Path, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

    def get(self, key: Path) -> asyncio.Lock:
        """The mutex for ``key``, created on first use.

        ``asyncio.Lock`` binds to the loop that first awaits it, so the loop is
        part of the entry: a lock left behind by a previous loop (a test's, or a
        restarted server's) can never be awaited again, and is replaced rather
        than reused. AgentHub runs one loop, so in production a given key takes
        this branch exactly once.
        """
        loop = asyncio.get_running_loop()
        entry = self._locks.get(key)
        if entry is not None and entry[0] is loop:
            return entry[1]

        lock = asyncio.Lock()
        self._locks[key] = (loop, lock)
        log.debug("worktree.lock_created", purpose=self._purpose, key=str(key))
        for stale, (owner, _) in list(self._locks.items()):
            if owner.is_closed():
                del self._locks[stale]
        return lock


# Serializes the merge sequence into one integration worktree (C5).
_MERGE_LOCKS = _LockRegistry("integration merge")

# Serializes `git worktree add|remove|prune` per repository, keyed by the git
# common dir so that every session opened on one repository shares it (C4).
_REGISTRY_LOCKS = _LockRegistry("worktree registration")


async def _common_dir(cwd: Path) -> Path:
    """The repository's ``.git`` shared by all of its worktrees.

    ``--path-format=absolute`` because the default prints ``.git`` relative to
    the cwd, and the *same* repository reached from two worktrees has to produce
    the same lock key. Resolved for the same reason.
    """
    result = await _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return await _real_path(Path(result.stdout.strip()))


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
    final_branch: str | None = None
    identity: GitIdentity = AGENT_IDENTITY

    @property
    def integration_branch(self) -> str:
        return f"{BRANCH_NAMESPACE}/{self.session_id}/{INTEGRATION}"

    @property
    def result_branch(self) -> str:
        return self.final_branch or f"{BRANCH_NAMESPACE}/{self.session_id}/result"

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

        Only the ``add`` is serialized (see :class:`_LockRegistry`); folding the
        remaining parents happens in this node's own worktree, contends with
        nobody, and stays outside the mutex.
        """
        path = self.node_path(node_id)
        branch = self.node_branch(node_id)
        parent_branches = [self.node_branch(p) for p in parents]
        source_ref = parent_branches[0] if parent_branches else self.integration_branch

        async with await self.registry_lock():
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
        cwd = path if path.exists() else self.repo_path
        base = await self._rev_parse(cwd, f"{base_ref}^{{commit}}")
        branch = self.node_branch(node_id)
        result = await self._git(
            cwd,
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

    async def integration_diff(self, *, base_refs: Sequence[str]) -> str:
        """Return the session's aggregate patch on the integration branch.

        Root nodes can be materialized from different integration checkpoints
        when scheduling is staggered. Their merge base is therefore the stable
        start of the generated result; diffing from a single final node would
        hide work contributed by its siblings.
        """
        if not base_refs:
            raise WorktreeError("an integration diff requires at least one base ref")
        # The target checkout is never removed. Reading refs from it avoids a
        # race with successful-session cleanup removing the integration
        # worktree while the completed-session drawer loads this diff.
        cwd = self.repo_path
        result_ref = (
            self.result_branch
            if await self._ref_exists(cwd, self.result_branch)
            else self.integration_branch
        )
        resolved = [
            await self._rev_parse(cwd, f"{ref}^{{commit}}") for ref in base_refs
        ]
        common = await self._git(
            cwd,
            "merge-base",
            "--octopus",
            *resolved,
            result_ref,
        )
        result = await self._git(
            cwd,
            "diff",
            "--no-ext-diff",
            "--binary",
            "--find-renames",
            "--end-of-options",
            common.stdout.strip(),
            result_ref,
            "--",
        )
        return result.stdout

    async def finalize(self, *, node_ids: Sequence[NodeId]) -> FinalizeResult:
        """Keep one result branch and remove a completed session's worktrees.

        Every managed worktree is checked for dirt before the first removal.
        The result ref is created first, so every commit remains reachable even
        if cleanup is interrupted halfway through. Repeating the operation is
        safe and finishes any cleanup left by such an interruption.
        """
        node_ids = tuple(node_ids)
        node_paths = tuple(self.node_path(node_id) for node_id in node_ids)
        cwd = (
            self.integration_path if self.integration_path.exists() else self.repo_path
        )

        if await self._ref_exists(cwd, self.integration_branch):
            commit = await self._rev_parse(cwd, self.integration_branch)
            if await self._ref_exists(cwd, self.result_branch):
                existing = await self._rev_parse(cwd, self.result_branch)
                if existing != commit:
                    raise BranchAlreadyExistsError(
                        f"final branch {self.result_branch!r} already exists at "
                        f"a different commit"
                    )
            else:
                await _require_available_branch(cwd, self.result_branch)
                await self._git(
                    cwd,
                    "branch",
                    self.result_branch,
                    self.integration_branch,
                )
        elif await self._ref_exists(cwd, self.result_branch):
            commit = await self._rev_parse(cwd, self.result_branch)
        else:
            raise WorktreeError(
                f"session {self.session_id} has neither integration nor result branch"
            )

        lock = _REGISTRY_LOCKS.get(await _common_dir(cwd))
        removed_worktrees: list[Path] = []
        async with lock:
            registered = set(await self._list_worktrees(cwd))
            managed_list: list[Path] = []
            for path in (*node_paths, self.integration_path):
                if await _real_path(path) in registered:
                    managed_list.append(path)
            managed = tuple(managed_list)
            for path in managed:
                dirty = await self._git(path, "status", "--porcelain")
                if dirty.stdout:
                    raise WorktreeError(f"refusing to finalize dirty worktree {path}")
            for path in managed:
                await self._git(
                    self.repo_path,
                    "worktree",
                    "remove",
                    "--",
                    str(path),
                )
                removed_worktrees.append(path)
            await self._git(self.repo_path, "worktree", "prune")

        removed_branches: list[str] = []
        # Node branches are durable per-attempt review history. Worktrees are
        # temporary; their refs are not. Only the superseded aggregate
        # integration ref is removed after the result ref points at the same
        # commit.
        for branch in (self.integration_branch,):
            if not await self._ref_exists(self.repo_path, branch):
                continue
            await self._git(self.repo_path, "branch", "-D", "--", branch)
            removed_branches.append(branch)
        try:
            await asyncio.to_thread(self.root.rmdir)
        except OSError:
            pass
        log.info(
            "worktree.session_finalized",
            session_id=self.session_id,
            branch=self.result_branch,
            commit=commit,
            worktrees=len(removed_worktrees),
            transient_branches=len(removed_branches),
        )
        return FinalizeResult(
            branch=self.result_branch,
            commit=commit,
            removed_worktrees=tuple(removed_worktrees),
            removed_branches=tuple(removed_branches),
        )

    async def commit(
        self,
        node_id: NodeId,
        message: str,
        *,
        base_ref: str | None = None,
    ) -> CommitResult:
        """Checkpoint a node's work, including commits authored by the agent.

        Harnesses are asked to edit the worktree, but coding agents sometimes
        also run ``git commit`` themselves. In that case the index is clean even
        though the node branch differs from its immutable base. ``base_ref``
        lets the orchestrator recognize that existing commit as a valid
        checkpoint instead of misclassifying a successful run as ``no_changes``.

        Callers that do not own a node base may omit it and retain the narrower
        "commit currently staged/unstaged files" behavior.
        """
        path = self.node_path(node_id)
        branch = self.node_branch(node_id)

        await self._git(path, "add", "--all", "--")
        staged = await self._git(path, "diff", "--cached", "--name-only", "-z", "--")
        changed = _split_nul_paths(staged.stdout)
        if not changed:
            if base_ref is not None:
                existing = await self._git(
                    path,
                    "diff",
                    "--name-only",
                    "-z",
                    "--end-of-options",
                    base_ref,
                    branch,
                    "--",
                )
                existing_paths = _split_nul_paths(existing.stdout)
                if existing_paths:
                    head = await self._rev_parse(path, "HEAD")
                    log.info(
                        "worktree.agent_commit_checkpointed",
                        session_id=self.session_id,
                        node_id=node_id,
                        commit=head,
                        files=len(existing_paths),
                    )
                    return CommitResult(
                        CommitStatus.CHECKPOINTED,
                        branch,
                        commit=head,
                        changed_paths=existing_paths,
                    )
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

    async def integration_lock(self) -> asyncio.Lock:
        """The mutex serializing merges into *this* session's integration
        worktree. Shared by every :class:`SessionWorkspace` value pointing at the
        same worktree; independent from any other session's."""
        return _MERGE_LOCKS.get(await _real_path(self.integration_path))

    async def registry_lock(self) -> asyncio.Lock:
        """The mutex serializing ``git worktree add|remove|prune`` on this
        *repository*. Shared with every other session opened on it, because
        ``.git/worktrees/`` is one directory (see :class:`_LockRegistry`)."""
        return _REGISTRY_LOCKS.get(await _common_dir(self.integration_path))

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

        Serialized per integration worktree. The lock covers the *whole*
        sequence — the up-to-date check, the merge, and the abort — because every
        step reads or writes the one shared index; releasing it between steps is
        what turns a second node's merge into a false conflict or a crash (see
        the module docstring). Node worktrees stay fully parallel; a merge is
        fast and the queue drains in arrival order, so a waiting node is delayed,
        never starved.
        """
        branch = self.node_branch(node_id)
        lock = await self.integration_lock()
        async with lock:
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
        lock = await self.registry_lock()
        # The check is inside the mutex — `list_worktrees` does not take it, so
        # there is no reentrancy to worry about, and reading the registry while
        # another node is registering itself is the race being avoided.
        async with lock:
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
        target repo itself and the integration worktree.

        Deliberately *not* behind the registration mutex: git tolerates a
        half-written entry here (measured), and taking it would deadlock
        :meth:`remove_node`, which calls this from inside that mutex.
        """
        cwd = (
            self.integration_path if self.integration_path.exists() else self.repo_path
        )
        return await self._list_worktrees(cwd)

    async def _list_worktrees(self, cwd: Path) -> tuple[Path, ...]:
        result = await self._git(cwd, "worktree", "list", "--porcelain")
        return _parse_worktree_list(result.stdout)

    async def _ref_exists(self, cwd: Path, ref: str) -> bool:
        result = await self._git(
            cwd,
            "rev-parse",
            "--verify",
            "--quiet",
            f"refs/heads/{ref}",
            ok_codes=(0, 1),
        )
        return result.returncode == 0

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


@dataclass(frozen=True, slots=True)
class _RepositoryTarget:
    repo: Path
    common_dir: Path
    base_commit: str


async def _repository_target(repo_path: Path, base_ref: str) -> _RepositoryTarget:
    """Resolve and validate a repository target without creating a worktree."""
    repo = await _real_path(repo_path)
    probe = await _git(
        repo,
        "rev-parse",
        "--path-format=absolute",
        "--git-common-dir",
        ok_codes=(0, 128),
    )
    if probe.returncode != 0:
        raise NotARepositoryError(f"{repo} is not a git repository: {probe.stderr}")
    common_dir = await _real_path(Path(probe.stdout.strip()))

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
    return _RepositoryTarget(
        repo=repo,
        common_dir=common_dir,
        base_commit=resolved_base.stdout.strip(),
    )


async def validate_repository(repo_path: Path, *, base_ref: str = "HEAD") -> Path:
    """Validate what a future session would target, without materializing it.

    Planning can take minutes and spend tokens. This preflight gives callers a
    read-only way to reject a missing repository or base ref before asking a
    model for a graph that can never run. Creation validates again because the
    repository may change while the model is answering.
    """
    return (await _repository_target(repo_path, base_ref)).repo


async def validate_final_branch(
    repo_path: Path,
    final_branch: str,
    *,
    base_ref: str = "HEAD",
) -> Path:
    """Reject an invalid or already occupied local result ref.

    This is a read-only preflight for planning. Creation repeats the check under
    the repository registration mutex because the repository can change while
    the planner is working. ``check-ref-format --branch`` is Git's own grammar;
    reimplementing its edge cases in Pydantic would eventually disagree with
    the command that creates the ref.
    """
    target = await _repository_target(repo_path, base_ref)
    await _validate_final_branch_name(target.repo, final_branch)
    async with _REGISTRY_LOCKS.get(target.common_dir):
        await _require_available_branch(target.repo, final_branch)
    return target.repo


async def init_session_workspace(
    *,
    repo_path: Path,
    session_id: SessionId,
    workspaces_root: Path | None = None,
    base_ref: str = "HEAD",
    final_branch: str | None = None,
    identity: GitIdentity = AGENT_IDENTITY,
) -> SessionWorkspace:
    """Create ``<workspaces_root>/<session_id>/integration`` and its branch.

    Idempotent: if the integration worktree is already registered (orchestrator
    restart) the existing workspace is returned untouched.
    """
    _valid_name(session_id, kind="session id")
    target = await _repository_target(repo_path, base_ref)
    repo = target.repo

    root = (workspaces_root or default_workspaces_root()) / session_id
    await asyncio.to_thread(root.mkdir, parents=True, exist_ok=True)
    workspace = SessionWorkspace(
        session_id=session_id,
        repo_path=repo,
        root=await _real_path(root),
        final_branch=final_branch,
        identity=identity,
    )
    if final_branch is not None and (
        workspace.integration_branch == final_branch
        or workspace.integration_branch.startswith(f"{final_branch}/")
        or final_branch.startswith(f"{workspace.integration_branch}/")
    ):
        raise InvalidBranchNameError(
            f"final branch {final_branch!r} conflicts with AgentHub's internal "
            f"branch namespace for session {session_id}"
        )

    integration = await _real_path(workspace.integration_path)
    # Check and add under the repository's registration mutex: two sessions
    # starting at once on one repository write to the same `.git/worktrees/`.
    async with _REGISTRY_LOCKS.get(target.common_dir):
        existing = await _git(repo, "worktree", "list", "--porcelain")
        if integration in _parse_worktree_list(existing.stdout):
            return workspace

        if final_branch is not None:
            await _validate_final_branch_name(repo, final_branch)
            await _require_available_branch(repo, final_branch)

        await _git(
            repo,
            "worktree",
            "add",
            "-b",
            workspace.integration_branch,
            "--",
            str(workspace.integration_path),
            target.base_commit,
        )
    log.info(
        "worktree.session_initialized",
        session_id=session_id,
        repo=str(repo),
        branch=workspace.integration_branch,
        base=target.base_commit,
    )
    return workspace


async def _validate_final_branch_name(repo: Path, branch: str) -> None:
    checked = await _git(
        repo,
        "check-ref-format",
        "--branch",
        branch,
        ok_codes=(0, 128),
    )
    if checked.returncode != 0 or checked.stdout.strip() != branch:
        raise InvalidBranchNameError(f"invalid Git branch name: {branch!r}")


async def _require_available_branch(repo: Path, branch: str) -> None:
    refs = await _git(
        repo,
        "for-each-ref",
        "--format=%(refname:strip=2)",
        "refs/heads",
    )
    for existing in refs.stdout.splitlines():
        # Git refs are a file tree. Besides an exact name, ``release`` and
        # ``release/v1`` conflict in either creation order and must be reported
        # as the same branch collision before ``git branch`` emits a raw 500.
        if (
            existing == branch
            or existing.startswith(f"{branch}/")
            or branch.startswith(f"{existing}/")
        ):
            raise BranchAlreadyExistsError(
                f"final branch {branch!r} conflicts with existing branch {existing!r}"
            )


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
