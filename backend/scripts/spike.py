#!/usr/bin/env python
"""Phase 0 vertical spike — one node, end to end, no UI.

Creates a git worktree, launches Claude Code inside an ai-jail sandbox, streams
the structured events, counts all four token fields, commits the result, merges
into the integration branch, and finally replays the NDJSON log to prove it
reproduces what was written.

This script is throwaway; Phase 1 replaces it with the scheduler. Everything it
calls is not — the parser, the sandbox policy, the worktree lifecycle, the event
log and the pricing table all live in the modules they keep permanently.

    uv run python scripts/spike.py ~/some/repo "add a docstring to foo()"

Nothing here talks to the target repository directly (invariant 2): the agent
only ever sees its own worktree.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from app.harnesses import claude_code
from app.harnesses.base import ParseStats, RunSpec
from app.harnesses.claude_code import ClaudeCodeAdapter
from app.harnesses.events import (
    AgentEvent,
    AssistantText,
    RunFinished,
    RunStarted,
    ThinkingDelta,
    ToolCall,
    ToolResult,
    TurnFinished,
    TurnStarted,
    Usage,
)
from app.models.clock import now_ms
from app.models.ids import new_run_id, new_session_id
from app.models.pricing import PriceTable, TokenCounts, load_price_table
from app.orchestrator import worktree
from app.sandbox import aijail
from app.storage.ndjson import EventLog, events_path, verify_roundtrip

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PRICING = REPO_ROOT / "pricing.yaml"
NODE_ID = "spike"

DIM = "\x1b[2m"
BOLD = "\x1b[1m"
RED = "\x1b[31m"
YELLOW = "\x1b[33m"
GREEN = "\x1b[32m"
RESET = "\x1b[0m"


def _c(text: str, colour: str) -> str:
    return text if os.environ.get("NO_COLOR") else f"{colour}{text}{RESET}"


def _rule(title: str) -> None:
    print(f"\n{_c('── ' + title + ' ' + '─' * max(0, 60 - len(title)), DIM)}")


def sandbox_launcher(policy: aijail.SandboxPolicy) -> tuple[str, ...]:
    """The ai-jail prefix that ``RunSpec.launcher`` expects.

    The two argv builders do not compose directly: ``aijail.build_argv`` emits
    the harness preset as a positional, and ``claude_code.build_argv`` appends
    its own ``claude`` after the launcher. They only line up because ai-jail's
    preset name and the CLI command happen to be the same string, so the preset
    is dropped here and re-added by the adapter.

    That seam is awkward and belongs in Phase 1's design, not in a builder —
    recorded in docs/phase-0.md rather than papered over.
    """
    argv = aijail.build_argv(policy, claude_code.CLI_COMMAND)
    if argv[-1] != claude_code.CLI_COMMAND:
        raise AssertionError(f"expected preset last in {argv!r}")
    return tuple(argv[:-1])


def render(event: AgentEvent) -> None:
    """Print an event the way a human reads a session."""
    match event:
        case RunStarted():
            print(
                _c(f"run {event.run_id}", BOLD),
                _c(f"{event.harness} · {event.model} · pid {event.pid}", DIM),
            )
        case TurnStarted():
            _rule(f"turn {event.turn}")
        case ThinkingDelta():
            print(_c(event.text.strip(), DIM))
        case AssistantText():
            print(event.text.strip())
        case ToolCall():
            summary = ", ".join(f"{k}={v!r}"[:60] for k, v in event.input.items())
            print(_c(f"  → {event.tool}({summary})", YELLOW))
        case ToolResult():
            if event.denied:
                mark, colour = "refused", RED
            elif event.ok:
                mark, colour = "ok", GREEN
            else:
                mark, colour = "error", RED
            print(_c(f"  ← {mark}: {event.preview.strip()[:120]}", colour))
        case Usage():
            note = "" if event.source == "reported" else f" ({event.source})"
            print(
                _c(
                    f"  tokens{note}: in={event.input_tokens} "
                    f"out={event.output_tokens} "
                    f"cache_read={event.cache_read_tokens} "
                    f"cache_write={event.cache_write_tokens}",
                    DIM,
                )
            )
        case TurnFinished():
            if event.blocked_by_permission:
                tools = ", ".join(d.tool for d in event.permission_denials)
                print(_c(f"  turn {event.turn}: REFUSED ({tools})", RED))
            else:
                print(_c(f"  turn {event.turn}: {event.status}", DIM))
            for err in event.errors:
                print(_c(f"  error: {err}", RED))
        case RunFinished():
            print(_c(f"process exited {event.exit_code} ({event.status})", DIM))
        case _:
            pass


def accumulate(events: Sequence[AgentEvent]) -> tuple[TokenCounts, str | None]:
    """Sum the four fields across every ``Usage`` (invariant 3)."""
    total = TokenCounts()
    model: str | None = None
    for event in events:
        if isinstance(event, Usage):
            model = event.model
            total = total + TokenCounts(
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
                cache_read_tokens=event.cache_read_tokens,
                cache_write_tokens=event.cache_write_tokens,
                cache_write_5m_tokens=event.cache_write_5m_tokens,
                cache_write_1h_tokens=event.cache_write_1h_tokens,
            )
    return total, model


def report(events: Sequence[AgentEvent], stats: ParseStats, prices: PriceTable) -> bool:
    """Print the summary. Returns False if the run must not be treated as done."""
    ok = True
    _rule("accounting")
    total, model = accumulate(events)
    print(
        f"  input        {total.input_tokens:>10,}\n"
        f"  output       {total.output_tokens:>10,}\n"
        f"  cache_read   {total.cache_read_tokens:>10,}\n"
        f"  cache_write  {total.cache_write_tokens:>10,}\n"
        f"  {'total':<12} {total.total:>10,}"
    )

    if model is None:
        print(_c("  no usage reported", YELLOW))
        ok = False
    else:
        cost = prices.cost_usd(model, total)
        if cost is None:
            print(_c(f"  cost: unknown — {model} is not in pricing.yaml", YELLOW))
        else:
            # Invariant 7: under a subscription there is no per-token billing.
            print(f"  estimated equivalent cost: ${cost:.4f} ({model})")

    _rule("parse")
    print(f"  {stats.lines} lines → {stats.events} events")
    if stats.ignored:
        print(_c(f"  ignored: {stats.ignored}", DIM))
    if stats.zero_usage_turns:
        print(_c(f"  turns with zero reported usage: {stats.zero_usage_turns}", YELLOW))
    if stats.usage_unreconciled_turns:
        print(
            _c(
                f"  UNRECONCILED usage: {stats.usage_unreconciled_turns} turn(s)",
                RED,
            )
        )
        ok = False
    if stats.unhandled:
        print(
            _c(f"  UNHANDLED: unknown={stats.unknown} malformed={stats.malformed}", RED)
        )
        print(_c("  a new line type means the parser is out of date", RED))
        ok = False

    refused = [
        e for e in events if isinstance(e, TurnFinished) and e.blocked_by_permission
    ]
    if refused:
        print(
            _c(
                "\n  the agent was REFUSED — this run reports success but did "
                "nothing. Never mark such a node done.",
                RED,
            )
        )
        return False

    finished = [e for e in events if isinstance(e, RunFinished)]
    return ok and bool(finished) and finished[-1].status == "success"


async def run_spike(
    *,
    repo: Path,
    prompt: str,
    model: str,
    workspaces_root: Path | None,
    runs_root: Path,
    prices: PriceTable,
    budget_usd: float | None,
) -> int:
    session_id = new_session_id()
    run_id = new_run_id()

    _rule("workspace")
    workspace = await worktree.init_session_workspace(
        repo_path=repo, session_id=session_id, workspaces_root=workspaces_root
    )
    node = await workspace.create_node(NODE_ID)
    print(f"  {workspace.integration_branch}\n  {node.path}")

    policy = aijail.default_policy()
    spec = RunSpec(
        run_id=run_id,
        cwd=node.path,
        prompt=prompt,
        model=model,
        launcher=sandbox_launcher(policy),
        max_budget_usd=budget_usd,
    )

    _rule("sandbox")
    print(_c("  " + " ".join(claude_code.build_argv(spec)), DIM))

    adapter = ClaudeCodeAdapter()
    collected: list[AgentEvent] = []
    log_path = events_path(runs_root, run_id)

    _rule("agent")
    started = now_ms()
    async with EventLog(log_path) as log:
        handle = await adapter.start(spec)
        try:
            async for event in adapter.events(handle):
                # NDJSON first, always. It is the source of truth (invariant 4);
                # anything derived may be rebuilt from it, so it must never be
                # the thing that is missing when the process dies.
                await log.append(event)
                collected.append(event)
                render(event)
        except asyncio.CancelledError:
            await adapter.kill(handle)
            raise
        except Exception:
            # A failed log write or renderer must not leave the subprocess tree
            # running after the driver has lost control of its stream.
            with contextlib.suppress(Exception):
                await adapter.kill(handle)
            raise
    print(_c(f"  {now_ms() - started} ms · {log.count} events → {log_path}", DIM))

    ok = report(collected, adapter.stats, prices)

    _rule("replay")
    # Invariant 4 stated as an assertion rather than an aspiration: if the log
    # cannot rebuild the stream now, no projection built on it is trustworthy.
    verify_roundtrip(log_path, collected)
    print(_c(f"  {log_path.name} reproduces all {len(collected)} events", GREEN))

    _rule("checkpoint")
    commit = await workspace.commit(NODE_ID, f"agent: {prompt[:60]}")
    print(f"  commit: {commit.status} ({len(commit.changed_paths)} files)")
    if not commit.committed:
        print(_c("  the agent changed nothing", YELLOW))
        ok = False
    elif not ok:
        print(_c("  run was not successful; node branch was not merged", RED))
    else:
        _rule("integrate")
        merge = await workspace.merge_into_integration(NODE_ID)
        print(f"  merge:  {merge.status}")
        if merge.blocked:
            print(_c(f"  conflicts: {[str(p) for p in merge.conflicts]}", RED))
            ok = False

    print()
    print(_c("  worktree kept for inspection: " + str(node.path), DIM))
    print(_c(f"  git -C {workspace.integration_path} log --oneline -3", DIM))
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", type=Path, help="target git repository")
    parser.add_argument("prompt", help="what the agent should do")
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--budget-usd", type=float, default=None)
    parser.add_argument("--workspaces-root", type=Path, default=None)
    parser.add_argument(
        "--runs-root", type=Path, default=Path.home() / ".agenthub" / "runs"
    )
    parser.add_argument("--pricing", type=Path, default=DEFAULT_PRICING)
    args = parser.parse_args(argv)

    prices = load_price_table(args.pricing)
    try:
        return asyncio.run(
            run_spike(
                repo=args.repo.expanduser().resolve(),
                prompt=args.prompt,
                model=args.model,
                workspaces_root=args.workspaces_root,
                runs_root=args.runs_root,
                prices=prices,
                budget_usd=args.budget_usd,
            )
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    with contextlib.suppress(BrokenPipeError):
        sys.exit(main())
