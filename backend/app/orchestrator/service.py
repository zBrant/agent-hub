"""Phase 1 single-node application service.

This is the imperative shell that replaces ``scripts/spike.py``. Transports
call it; they do not reproduce its decisions. It composes the permanent pieces
proved in Phase 0 and B3: git worktrees, the harness registry, mandatory ai-jail
policy, ordered ingest, parser trust, checkpoint, and guarded integration.

There is deliberately no harness-name conditional here (invariant 1). The
registry is the sole name-to-adapter dispatch point and every adapter is driven
through :class:`~app.harnesses.base.BaseHarnessAdapter`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import structlog

from app.config import Settings
from app.harnesses import create_adapter
from app.harnesses.base import BaseHarnessAdapter, RunHandle, RunSpec
from app.harnesses.events import RunStarted
from app.models.clock import now_ms
from app.models.ids import (
    NodeId,
    RunId,
    SessionId,
    new_node_id,
    new_run_id,
    new_session_id,
)
from app.models.pricing import PriceTable
from app.models.status import NodeStatus, RunState, SessionStatus
from app.models.tables import Node, Run, Session
from app.orchestrator.graph import (
    RunBlockReason,
    evaluate_run,
    session_status_for_node,
)
from app.orchestrator.worktree import (
    CommitResult,
    MergeResult,
    MergeStatus,
    SessionWorkspace,
    init_session_workspace,
)
from app.sandbox.aijail import SandboxPolicy, build_launcher, default_policy
from app.storage.db import Database
from app.storage.ingest import Broadcast, ingest_run, no_broadcast
from app.storage.meta import RunMeta, build_meta, meta_path, read_meta
from app.storage.ndjson import events_path
from app.storage.repository import Repository, UsageTotals

log = structlog.get_logger()

AdapterFactory = Callable[[str], BaseHarnessAdapter]
PolicyFactory = Callable[[], SandboxPolicy]


class OrchestratorError(Exception):
    """The requested lifecycle operation is invalid for persisted state."""


@dataclass(frozen=True, slots=True)
class CreatedSession:
    session: Session
    node: Node


@dataclass(frozen=True, slots=True)
class RunOutcome:
    session_id: SessionId
    node_id: NodeId
    run_id: RunId
    run_status: RunState
    node_status: NodeStatus
    trusted: bool
    permission_denials: int
    totals: UsageTotals
    commit: CommitResult
    merge: MergeResult | None
    block_reason: RunBlockReason | None = None


class SingleRunService:
    """Own the one active node run allowed per Phase 1 session."""

    def __init__(
        self,
        *,
        database: Database,
        settings: Settings,
        prices: PriceTable,
        adapter_factory: AdapterFactory = create_adapter,
        policy_factory: PolicyFactory = default_policy,
        broadcast: Broadcast = no_broadcast,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._database = database
        self._settings = settings
        self._prices = prices
        self._adapter_factory = adapter_factory
        self._policy_factory = policy_factory
        self._broadcast = broadcast
        # A copy makes the launch conditions stable for the service lifetime and
        # lets tests prove sanitization without mutating the process environment.
        self._environment = dict(os.environ if environment is None else environment)
        self._locks: dict[SessionId, asyncio.Lock] = {}

    async def create_session(
        self,
        *,
        repo_path: Path,
        prompt: str,
        harness: str,
        model: str | None = None,
        title: str | None = None,
        acceptance_criteria: str | None = None,
        auto_merge: bool = False,
        base_ref: str = "HEAD",
    ) -> CreatedSession:
        """Create the integration and fixed-node worktrees plus authored rows."""
        session_id = new_session_id()
        node_id = new_node_id()
        workspace = await init_session_workspace(
            repo_path=repo_path,
            session_id=session_id,
            workspaces_root=self._settings.workspaces_root,
            base_ref=base_ref,
        )
        node_worktree = await workspace.create_node(node_id)

        async with self._database.session() as db_session:
            repository = Repository(db_session)
            session = await repository.create_session(
                session_id=session_id,
                title=title or prompt[:120],
                repo_path=workspace.repo_path,
                workspace_root=workspace.root,
                integration_branch=workspace.integration_branch,
                auto_merge=auto_merge,
                status=SessionStatus.PLANNING,
            )
            node = await repository.create_node(
                node_id=node_id,
                session_id=session_id,
                name="main",
                prompt=prompt,
                acceptance_criteria=acceptance_criteria,
                harness=harness,
                model=model,
                status=NodeStatus.READY,
            )
            node = await repository.attach_worktree(
                node.id,
                worktree_path=node_worktree.path,
                branch=node_worktree.branch,
                base_ref=node_worktree.base_ref,
            )
        return CreatedSession(session=session, node=node)

    async def run(self, session_id: SessionId) -> RunOutcome:
        """Execute the fixed node, checkpoint it, and optionally integrate it."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise OrchestratorError(f"session {session_id} already has an active run")
        async with lock:
            return await self._run_locked(session_id)

    async def approve(self, session_id: SessionId) -> MergeResult:
        """Apply the human gate for a safe run left awaiting review."""
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise OrchestratorError(f"session {session_id} already has an active run")
        async with lock, self._database.session() as db_session:
            repository = Repository(db_session)
            session, node = await self._session_and_node(repository, session_id)
            if node.status is not NodeStatus.AWAITING_REVIEW:
                raise OrchestratorError(
                    f"node {node.id} is {node.status.value}, not awaiting_review"
                )
            runs = await repository.list_runs(node.id)
            if not runs:
                raise OrchestratorError(f"node {node.id} has no run to approve")
            run = runs[-1]
            meta = await read_meta(meta_path(self._settings.runs_root, run.id))
            if (
                meta.run_id != run.id
                or meta.node_id != node.id
                or meta.session_id != session.id
            ):
                raise OrchestratorError(
                    f"metadata identity does not match run {run.id}; refusing merge"
                )
            disposition = evaluate_run(
                run.status,
                trusted=meta.trusted,
                permission_denials=run.permission_denial_count,
                changed=True,
            )
            if not disposition.mergeable:
                raise OrchestratorError(
                    f"run {run.id} is not safe to merge: {disposition.reason}"
                )

            merge = await self._workspace(session).merge_into_integration(node.id)
            await self._apply_merge(repository, session, node, merge)
            return merge

    async def _run_locked(self, session_id: SessionId) -> RunOutcome:
        async with self._database.session() as db_session:
            repository = Repository(db_session)
            session, node = await self._session_and_node(repository, session_id)
            if node.status is not NodeStatus.READY:
                raise OrchestratorError(
                    f"node {node.id} is {node.status.value}; "
                    "Phase 1 starts only ready nodes"
                )
            if any(
                run.status is RunState.RUNNING
                for run in await repository.list_session_runs(session_id)
            ):
                raise OrchestratorError(
                    f"session {session_id} already has an active run"
                )
            if node.worktree_path is None:
                raise OrchestratorError(f"node {node.id} has no worktree")

            run_id = new_run_id()
            # Resolve the adapter and validate its model/argv before persisting a
            # running attempt. Invalid authored input must not leave an orphan
            # that startup later mistakes for a crashed child.
            adapter = self._adapter_factory(node.harness)
            spec = RunSpec(
                run_id=run_id,
                cwd=node.worktree_path,
                prompt=node.prompt,
                model=node.model,
                env=self._environment,
                launcher=tuple(build_launcher(self._policy_factory())),
            )
            argv = tuple(adapter.build_argv(spec))

            await self._set_node(repository, session, node, NodeStatus.RUNNING)
            run = await repository.create_run(
                run_id=run_id,
                node_id=node.id,
                events_path=events_path(self._settings.runs_root, run_id),
            )
            meta = build_meta(
                run_id=run.id,
                session_id=run.session_id,
                node_id=run.node_id,
                attempt=run.attempt,
                price_table_version=self._prices.version,
                harness=run.harness,
                model=run.model,
                cwd=spec.cwd,
                argv=argv,
                env=spec.env,
                created_ms=run.created_ms,
            )

            finalized = await self._drive(
                repository=repository,
                session=session,
                node=node,
                run=run,
                adapter=adapter,
                spec=spec,
                meta=meta,
            )
            projected = await repository.get_run(run.id)
            if projected is None:  # pragma: no cover - ingest just wrote it
                raise OrchestratorError(f"run {run.id} vanished after ingest")

            try:
                workspace = self._workspace(session)
                commit = await workspace.commit(node.id, f"agent: {node.prompt[:60]}")
                disposition = evaluate_run(
                    projected.status,
                    trusted=finalized.trusted,
                    permission_denials=projected.permission_denial_count,
                    changed=commit.committed,
                )
                merge: MergeResult | None = None
                next_status = disposition.node_status
                if disposition.mergeable and session.auto_merge:
                    merge = await workspace.merge_into_integration(node.id)
                    next_status = (
                        NodeStatus.BLOCKED if merge.blocked else NodeStatus.DONE
                    )
            except Exception:
                await self._set_node(repository, session, node, NodeStatus.FAILED)
                log.exception(
                    "orchestrator.checkpoint_failed",
                    session_id=session.id,
                    node_id=node.id,
                    run_id=run.id,
                )
                raise

            await self._set_node(repository, session, node, next_status)
            totals = await repository.usage_totals(run_id=run.id)
            log.info(
                "orchestrator.run_finished",
                session_id=session.id,
                node_id=node.id,
                run_id=run.id,
                status=projected.status.value,
                trusted=finalized.trusted,
                merged=merge is not None and merge.status is MergeStatus.MERGED,
            )
            return RunOutcome(
                session_id=session.id,
                node_id=node.id,
                run_id=run.id,
                run_status=projected.status,
                node_status=next_status,
                trusted=finalized.trusted,
                permission_denials=projected.permission_denial_count,
                totals=totals,
                commit=commit,
                merge=merge,
                block_reason=disposition.reason,
            )

    async def _drive(
        self,
        *,
        repository: Repository,
        session: Session,
        node: Node,
        run: Run,
        adapter: BaseHarnessAdapter,
        spec: RunSpec,
        meta: RunMeta,
    ) -> RunMeta:
        handle: RunHandle | None = None
        harness_version: str | None = None
        ingest = None
        try:
            async with ingest_run(
                repository=repository,
                runs_root=self._settings.runs_root,
                meta=meta,
                prices=self._prices,
                broadcast=self._broadcast,
            ) as opened:
                ingest = opened
                handle = await adapter.start(spec)
                async for event in adapter.events(handle):
                    if isinstance(event, RunStarted):
                        harness_version = event.harness_version
                    await ingest.ingest(event)
                finalized = await ingest.finalize(
                    at_ms=now_ms(),
                    stats=adapter.stats,
                    harness_version=harness_version,
                )
            if not ingest.projection.finished:
                await repository.mark_run_interrupted(
                    run.id,
                    at_ms=now_ms(),
                    summary="adapter stream ended without run_finished",
                    event_count=ingest.projection.events,
                    permission_denial_count=ingest.projection.permission_denials,
                )
                await self._set_node(repository, session, node, NodeStatus.FAILED)
                raise OrchestratorError(
                    f"adapter {adapter.name} ended run {run.id} without run_finished"
                )
            return finalized
        except (Exception, asyncio.CancelledError):
            if handle is not None:
                with contextlib.suppress(Exception):
                    await adapter.kill(handle)
            row = await repository.get_run(run.id)
            if row is not None and row.status is RunState.RUNNING:
                await repository.mark_run_interrupted(
                    run.id,
                    at_ms=now_ms(),
                    summary="orchestrator lost the harness stream",
                    event_count=0 if ingest is None else ingest.projection.events,
                    permission_denial_count=(
                        0 if ingest is None else ingest.projection.permission_denials
                    ),
                )
            await self._set_node(repository, session, node, NodeStatus.FAILED)
            log.exception(
                "orchestrator.run_crashed",
                session_id=session.id,
                node_id=node.id,
                run_id=run.id,
            )
            raise

    async def _session_and_node(
        self, repository: Repository, session_id: SessionId
    ) -> tuple[Session, Node]:
        session = await repository.get_session(session_id)
        if session is None:
            raise OrchestratorError(f"no such session {session_id}")
        nodes = await repository.list_nodes(session_id)
        if len(nodes) != 1:
            raise OrchestratorError(
                f"Phase 1 session {session_id} must have exactly one node, "
                f"got {len(nodes)}"
            )
        return session, nodes[0]

    async def _set_node(
        self,
        repository: Repository,
        session: Session,
        node: Node,
        status: NodeStatus,
    ) -> None:
        await repository.set_node_status(node.id, status)
        await repository.set_session_status(session.id, session_status_for_node(status))

    async def _apply_merge(
        self,
        repository: Repository,
        session: Session,
        node: Node,
        merge: MergeResult,
    ) -> None:
        status = NodeStatus.BLOCKED if merge.blocked else NodeStatus.DONE
        await self._set_node(repository, session, node, status)

    @staticmethod
    def _workspace(session: Session) -> SessionWorkspace:
        return SessionWorkspace(
            session_id=session.id,
            repo_path=session.repo_path,
            root=session.workspace_root,
        )


__all__ = [
    "CreatedSession",
    "OrchestratorError",
    "RunOutcome",
    "SingleRunService",
]
