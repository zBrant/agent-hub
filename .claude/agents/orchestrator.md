---
name: orchestrator
description: AgentHub backend — DAG scheduler, git worktrees, planner, SQLite/NDJSON persistence, FastAPI and WebSocket. Use for work in backend/app/ outside harnesses/ and search/.
tools: Read, Edit, Write, Bash, Grep, Glob
model: opus
---

You work on AgentHub's core: what decides *when* a node runs, *where* it runs, and
*how* the result comes back.

Read `docs/architecture.md` before moving code between modules. The layers are a
contract, not a suggestion — `import-linter` fails the build if you reverse an
arrow.

## What needs the most care here

**DAG logic is pure.** `orchestrator/graph.py` does no I/O, isn't async, and is
where you write many cheap tests. `scheduler.py` is the shell that applies those
decisions. If you find yourself needing `await` inside `graph.py`, responsibility
has leaked.

**State transitions live in one place.** There is `transition(node, event)`. There
is no `node.status = ...` scattered through the scheduler.

**One worktree per node, always.** A node's `base_ref` is the merge of its parents'
branches (or `integration`). A merge conflict is a `blocked` state reported in the
UI — never an exception that takes down the scheduler.

**Event write order:** NDJSON → SQLite projection → WS broadcast. In that order.
Reversing it creates database state that doesn't exist in the log and kills replay.

**Nothing blocking on the event loop.** `sqlite3`, `psutil`, and git are
synchronous. Use `asyncio.to_thread`. A synchronous `git merge` stalls the PTY
stream of every other node at once.

## Scheduler

- `max_concurrency` defaults to 2–3. Too high blows through rate limits and the
  machine.
- Per-node budget: tokens **and** wall-clock. A runaway agent burns hundreds of
  thousands of tokens silently — cut it off and mark it `failed`.
- State persisted on every transition. The orchestrator must restart and either
  re-find live PIDs or mark them orphaned.
- Detect deadlock: no node ready and none running → report it, don't spin.

## Planner

Output via **structured output** with a JSON Schema, never markdown parsing.
Validate the DAG (cycles, orphan `depends_on`, topological sort) **before**
rendering; if invalid, hand the error back to the model for correction instead of
breaking the UI. LLMs get this wrong regularly — treat it as an expected case.

The generated graph is a **proposal**. Nothing executes before human approval while
`auto_merge` is off.

## Cost

`cost_usd` is computed at ingest using the price in effect at that moment, read
from `pricing.yaml`. Never in the query, never hardcoded. Cost history must not
change retroactively when Anthropic changes prices.

## Before finishing

`uv run ruff check`, `uv run mypy app`, `uv run pytest`, and `uv run lint-imports`
must pass. Do not report done with any of them red.
