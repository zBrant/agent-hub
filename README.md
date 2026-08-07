# AgentHub

**A local orchestrator for AI coding agents.** Describe a goal, get a dependency
graph of activities, assign a harness and model to each node, and run them in
parallel — each agent sandboxed, each in its own git worktree, all of it streamed
back with live output, tool calls, and token accounting.

---

## The idea

Running one coding agent is easy. Running five at once against the same repository
is where it falls apart: they overwrite each other's edits, you can't audit the
diff, you can't tell which one burned 400K tokens, and you can't see what any of
them is actually doing.

AgentHub treats the **dependency graph as the unit of execution**, not as a
picture drawn next to a task list:

```mermaid
flowchart LR
    P[Planning chat] -->|structured output| G[DAG proposal]
    G -->|you assign harness + model,<br/>edit, approve| S[Scheduler]
    S --> A["node_a<br/>worktree + sandbox"]
    S --> B["node_b<br/>worktree + sandbox"]
    A --> C["node_c<br/>worktree + sandbox"]
    B --> C
    C --> M[Integration branch]
```

Each node gets its own git worktree branched off the merge of its parents, so
parallel agents produce independent, reviewable diffs. Each node runs under
[ai-jail](https://github.com/akitaonrails/ai-jail), an OS-level sandbox
(bubblewrap + Landlock on Linux, seatbelt on macOS) — not containers, so startup
is in milliseconds and harness auth keeps working.

Nothing executes before you approve the graph.

## Three surfaces

| Tab | What it does |
|---|---|
| **Dashboard** | Token and cost KPIs, system health (CPU, RAM, disk, per-agent process tree), **active** sessions |
| **Sessions** | Planning chat, graph orchestration, per-node drawer: edit before running, message the agent while running, review the diff after. **All** sessions |
| **Code Search** | Agentic search over your codebase — ripgrep, ast-grep, and tree-sitter symbols as *tools* an agent drives, not top-k RAG |

The structured harness channel remains the source of truth for state and tokens.
A real terminal (`xterm.js` over a PTY) is planned only for adapters that can
prove attachment to the same live runtime. Codex Channel B is currently
deferred; see [`docs/architecture.md`](docs/architecture.md#codex-attach-classification-validated-2026-08-06).

## Harnesses

Claude Code and Codex are the current adapters, behind a single contract.
OpenCode is planned. Every CLI normalizes into one `AgentEvent` stream, so
nothing downstream of the adapter layer knows which harness is running.

Adding one is a documented procedure: see
[`.claude/skills/add-harness/SKILL.md`](.claude/skills/add-harness/SKILL.md).

## Stack

**Backend** — Python 3.12+, FastAPI + asyncio, SQLite (WAL) + SQLModel, NDJSON
event log, uv, ruff, mypy.

**Frontend** — Vite + React 19 + TypeScript, shadcn/ui on Base UI + Tailwind,
TanStack Query + Zustand. Graph, terminal, and chart packages land with the
phases that first use them.

**Isolation** — ai-jail for the process sandbox, git worktrees for workspace
isolation.

No Docker in the MVP. No Postgres. No auth — it binds to `127.0.0.1` and is meant
to run on your own machine.

## Installation

Phase 1 is runnable from this checkout. It provides one persistent session,
one fixed node, real harness execution, live structured events, kill/retry,
manual approval, diff inspection, and deterministic replay. Planner graphs,
dashboards, metrics, and code search belong to later phases.

### Requirements

- macOS (the accepted path; Linux sandbox support is not yet accepted here)
- Python 3.12+ and [uv](https://github.com/astral-sh/uv)
- Node 20+ and pnpm
- [ai-jail](https://github.com/akitaonrails/ai-jail)
- Git
- An installed and authenticated Codex or Claude Code CLI

Install the locked dependencies:

```bash
git clone https://github.com/zBrant/agent-hub.git
cd agent-hub

cd backend
uv sync

cd ../frontend
pnpm install
```

Run the backend and frontend in separate terminals from the repository root:

```bash
# terminal 1
cd backend
uv run agenthub serve

# terminal 2
cd frontend
pnpm dev
```

Open <http://127.0.0.1:5173>. Both servers bind only to loopback. The Vite
server proxies `/api` and `/ws` to AgentHub on `127.0.0.1:8000`.

### Create and run a session

Phase 1 creates sessions through the resource API. Replace `/absolute/repo`
with an existing Git repository:

```bash
curl --request POST http://127.0.0.1:8000/api/sessions \
  --header 'Content-Type: application/json' \
  --data-binary @- <<'JSON'
{
    "repo_path": "/absolute/repo",
    "prompt": "Implement the requested change and run its tests.",
    "harness": "codex",
    "model": "gpt-5.6-terra",
    "auto_merge": false
}
JSON
```

Open `/sessions/<session_id>` in the frontend. The page exposes start, kill,
retry, approval, event history, all four token fields, estimated equivalent
cost, and the final diff. With `auto_merge=false`, a successful run stops at
`awaiting_review` until **Approve** is selected.

### Runtime data and replay

By default AgentHub stores `agenthub.db`, NDJSON logs, and worktrees under
`~/.agenthub/`. Set `AGENTHUB_ROOT` before starting the backend to use another
location.

Stop active work before an offline repair, then rebuild one run's SQLite
projection from its source log:

```bash
cd backend
uv run agenthub replay <run_id>
```

Use `--root /path/to/root` when the server ran with a non-default root. Replay
preserves authored sessions/nodes and uses the price-table version pinned at
ingest; it refuses rather than silently repricing when that table is missing.

### Verification

```bash
cd backend
uv run ruff format --check .
uv run ruff check .
uv run mypy app
uv run pytest
uv run lint-imports

cd ../frontend
pnpm test
pnpm typecheck
pnpm lint
pnpm build
pnpm gen:api --check
```

The real Codex HTTP/WebSocket/kill/retry/replay evidence is committed in
[`docs/acceptance-phase-1.md`](docs/acceptance-phase-1.md).

## A note on cost

When a harness runs under a Claude Max/Pro subscription there is no per-token
billing. AgentHub still counts tokens — all four fields, including `cache_read`,
which is 90%+ of a long agentic session — but labels the result **"estimated
equivalent cost"** rather than spend. A number that alarms without meaning
anything is worse than no number.

## Documentation

| Document | Answers |
|---|---|
| [`design.md`](design.md) | **What** to build and why — the full design: isolation, harness channels, token accounting, data model, the three tabs, the scheduler |
| [`docs/architecture.md`](docs/architecture.md) | **How** the code is organized — layers, the `AgentEvent` boundary, pure core / imperative shell, persistence, testing strategy |
| [`docs/conventions.md`](docs/conventions.md) | Python and TypeScript standards, naming, commits, security |
| [`docs/design-system.md`](docs/design-system.md) | Tokens, density, node states, terminal theme, accessibility |
| [`docs/acceptance-phase-1.md`](docs/acceptance-phase-1.md) | Real Phase 1 HTTP, WebSocket, retry, replay, and totals evidence |
| [`docs/roadmap.md`](docs/roadmap.md) | What is built, what isn't, and what comes next |
| [`CLAUDE.md`](CLAUDE.md) | The eight invariants, for humans and agents alike |

The repository ships its own agent configuration in
[`.claude/`](.claude) — four subagents (`orchestrator`, `harness-integrator`,
`ui`, `reviewer`) and two skills. AgentHub is built the way it expects you to
build with it.

## License

[MIT](LICENSE) © 2026 Eduardo Brant
