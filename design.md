# AgentHub — Architecture & MVP Design

> A local, single-user platform that orchestrates multiple AI coding agents over a
> dependency graph, with code search and token/system dashboards.
>
> This document defines **what** to build and **why**. For how the code is
> organized, see `docs/architecture.md`.

---

## 1. Overview

You describe a goal in a chat. A planner turns it into a **directed acyclic graph
of activities**. You review the graph, assign a harness and a model to each node,
and approve it. The scheduler then runs the ready nodes in parallel — each inside
its own git worktree, each sandboxed — and streams everything back: live output,
tool calls, token usage, cost.

Three surfaces, and that's the whole MVP:

| Tab | Purpose |
|---|---|
| **Dashboard** | Token/cost KPIs, system health, **active** sessions only |
| **Sessions** | Planning chat, graph orchestration, per-node interaction. **All** sessions |
| **Code Search** | Agentic chat over the codebase: business rules, "where is X handled" |

Design constraints that shape every decision below:

- **Single user, local.** Binds to `127.0.0.1`. No auth, no multi-tenancy, no
  remote execution in the MVP.
- **macOS first.** Linux should work, but the sandbox path is validated on Darwin.
- **The graph is the unit of execution**, not a visualization bolted onto a task
  list. If the graph can't safely run nodes in parallel, the product has no reason
  to exist.

---

## 2. Isolation: two problems, two mechanisms

Running several agents at once against one repository raises two independent
problems, and conflating them is the most common way to get this wrong.

| Problem | Mechanism |
|---|---|
| An agent reads `~/.aws`, exfiltrates `.env`, or runs something destructive | **Process sandbox** — ai-jail |
| Three agents edit the same files and produce an unauditable diff | **Workspace isolation** — git worktree |

A sandbox does not solve write concurrency. A worktree does not solve security.
You need both.

### 2.1 Process sandbox: ai-jail

[ai-jail](https://github.com/akitaonrails/ai-jail) is a Rust CLI that wraps agent
harnesses in OS-level sandboxes. It is **not** container-based:

| | |
|---|---|
| Language | Rust |
| Linux | `bubblewrap` (namespaces) + Landlock LSM + seccomp-bpf |
| macOS | `sandbox-exec` (seatbelt profiles) |
| Built-in harnesses | Claude Code, Codex, OpenCode, Crush, Pi CLI, Gemini CLI |
| Config | `.ai-jail` (TOML) per project + `~/.ai-jail` global |

Why this beats Docker for this product:

- **No container overhead.** On macOS, Docker is a VM: slow bind mounts, image
  rebuilds on every toolchain change. Startup is in milliseconds, which matters
  when a graph has 15 nodes.
- **Harness auth works for free.** ai-jail selectively mounts `~/.claude`,
  `~/.codex`, etc. The agent is already logged in. With Docker you'd have to
  re-inject credentials into every container.

Docker stays reserved for the visual-testing environment (Playwright + Chromium),
which is a long-lived service rather than a per-agent sandbox — and that is
post-MVP anyway (§7).

Per-node invocation the orchestrator assembles:

```
ai-jail \
  --worktree \                      # expose git metadata for linked worktrees
  --mask .env --mask '*.pem' \      # hide secrets from the agent
  --deny-path ~/.aws --deny-path ~/.ssh \
  --no-docker --no-gpu \            # no unnecessary passthrough
  claude
```

The policy is **default-deny and always explicit**. An empty sandbox policy is a
bug, not a configuration choice.

### 2.2 Workspace isolation: one git worktree per node

```
~/.agenthub/workspaces/
  sess_01H.../
    integration/          → branch agenthub/sess_01H/integration
    node_a/               → worktree, branch agenthub/sess_01H/node_a
    node_b/               → worktree, branch agenthub/sess_01H/node_b
```

The integration branch is `agenthub/<sess>/integration`, not `agenthub/<sess>`.
Git refs are files on disk, so a branch named `agenthub/sess_01H` makes the
directory `refs/heads/agenthub/sess_01H/` unusable and every node branch under
it fails to create. `integration` is therefore a reserved node id.

Per-node lifecycle:

1. `git worktree add -b agenthub/<sess>/<node> -- <path> <base_ref>`, where
   `base_ref` is the first parent's branch, or the integration branch for a root
   node. **A multi-parent node has no single base ref** — `git worktree add`
   takes one commit-ish. The remaining parents are merged in inside the fresh
   worktree, and folding stops at the first conflict, so a node whose parents
   cannot be combined comes back `blocked` before any agent is launched.
2. Run the agent inside the worktree, via ai-jail with `--worktree`.
3. On completion: `git commit` → merge (or rebase) into the integration branch.
4. A merge conflict becomes the node's `blocked` state, surfaced in the UI for
   manual resolution or for a resolver agent. The merge is **aborted** rather
   than left in place: a shared integration worktree stuck in `MERGING` would
   block every other node behind one human. The conflicting paths travel in the
   result, and resolution is an explicit later operation.

Two behaviors that exit codes do not express, and that any reimplementation will
get wrong once: `git merge` exits 0 on "Already up to date", and `git commit`
exits 1 when nothing is staged — indistinguishable from a real failure. Both
must be decided *before* invoking the command, not parsed out of its output.

This buys, for free: per-node diff, per-node rollback, clean retry, and human
review before integration. **It is what turns the graph from a pretty picture into
a real execution unit.** Without it, parallelism is unusable.

If the target project is not a git repository, degrade to an rsync copy plus diff —
but git is the correct path.

---

## 3. Talking to harnesses

This is the most underestimated part of the system. Reading a harness's raw
stdout while it runs in TUI mode gives you garbage: ANSI redraw sequences, cursor
addressing, alternate screen buffer. You cannot derive state from that.

There are **two distinct channels**, and you need both, for different reasons.

### Channel A — Structural (source of truth for state, tokens, tool calls)

Non-interactive mode with line-delimited JSON output:

```bash
claude -p --output-format stream-json --input-format stream-json --verbose
```

This yields typed events: `system/init`, `assistant` (carrying `message.usage`),
`user` (tool results), `result` (totals and cost). And `--input-format stream-json`
allows **injecting messages mid-session** — which is exactly the "interact with
this specific node's chat" requirement.

Equivalents in other harnesses (verify the flags against the installed version;
they change fast):

- Codex: `codex exec --json`
- OpenCode: server mode (`opencode serve`) with an HTTP/SSE API

### Channel B — PTY (visual fidelity, "just like the CLI")

To see the TUI exactly as it looks in a terminal, allocate a real PTY
(`os.openpty()` / `ptyprocess`) and bridge the bytes to the browser over a
WebSocket into `xterm.js`. Without a PTY the harness detects it is not a tty and
changes behavior.

**Channel A is the default** — dashboards, tokens, graph state, persisted history.
**Channel B is an "Attach terminal" button** per node only when the adapter has
proved that a PTY client attaches to the *same live harness runtime*. Never
extract state from Channel B. A CLI that can resume stored history in a second
process has continuation, not live attach; presenting that process as the
running node would be false and could put edits outside the run lifecycle.

For Codex 0.146.0, `codex app-server` plus `codex --remote` is an interactive
shared-session topology, but `codex exec --json` is not a client of that
app-server and exposes no attach transport. Phase 1 keeps the stable
`exec --json` Channel A adapter and therefore defers Codex Channel B. Enabling it
requires app-server to become the adapter's primary runtime first; app-server's
WebSocket surface is currently documented as experimental and unsupported.

### Adapter contract

```python
class BaseHarnessAdapter(Protocol):
    name: str
    supported_models: list[str]

    async def start(self, spec: RunSpec) -> RunHandle: ...
    async def send(self, handle: RunHandle, text: str) -> None: ...
    async def interrupt(self, handle: RunHandle) -> None: ...
    async def kill(self, handle: RunHandle) -> None: ...
    def events(self, handle: RunHandle) -> AsyncIterator[AgentEvent]: ...
```

Every adapter normalizes to a single `AgentEvent`:

```python
AgentEvent =
  | RunStarted(run_id, harness, model, cwd, pid)
  | AssistantText(delta: str)
  | ThinkingDelta(delta: str)
  | ToolCall(tool: str, input: dict, call_id: str)
  | ToolResult(call_id: str, ok: bool, preview: str)
  | Usage(input, output, cache_read, cache_write, model)
  | Permission(request_id, description)   # human gate
  | RunFinished(status, exit_code, summary)
  | RawChunk(bytes)                        # Channel B only
```

That single type is what lets the rest of the system — dashboard, graph, cost —
not care which CLI is running. It is the load-bearing boundary of the codebase;
`docs/architecture.md` §2 covers the rules that keep it intact.

---

## 4. Token accounting

If you sum only `input_tokens`, the dashboard reports an absurdly low number. The
API returns **four** fields, and they do not add up naively:

| Field | Relative cost |
|---|---|
| `input_tokens` | 1.0× (full price) |
| `cache_creation_input_tokens` | ~1.25× (5 min TTL) / ~2.0× (1 h TTL) |
| `cache_read_input_tokens` | ~0.1× |
| `output_tokens` | output price |

Total prompt tokens = `input + cache_creation + cache_read`. In a long agentic
session, 90%+ of tokens are `cache_read` — ignore it and the dashboard shows 4K
tokens for a session that consumed 400K.

```sql
CREATE TABLE usage_event (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  node_id TEXT,
  session_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts INTEGER NOT NULL,
  harness TEXT NOT NULL,
  model TEXT NOT NULL,
  source TEXT NOT NULL,       -- reported | reconstructed
  input_tokens INTEGER DEFAULT 0,
  output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0,
  cache_write_tokens INTEGER DEFAULT 0,
  cache_write_5m_tokens INTEGER DEFAULT 0,
  cache_write_1h_tokens INTEGER DEFAULT 0,
  price_table_version INTEGER NOT NULL,
  cost_usd REAL,              -- computed at ingest, with the price in effect then
  UNIQUE(run_id, seq)
);
CREATE INDEX ix_usage_session_ts ON usage_event(session_id, ts);
```

Compute `cost_usd` **at ingest time** (a price snapshot), never in the query.
Prices change and you do not want cost history to shift retroactively.

Current Anthropic model IDs and pricing:

| Model | ID | Input $/1M | Output $/1M |
|---|---|---|---|
| Claude Opus 5 | `claude-opus-5` | 5.00 | 25.00 |
| Claude Sonnet 5 | `claude-sonnet-5` | 3.00 | 15.00 |
| Claude Haiku 4.5 | `claude-haiku-4-5` | 1.00 | 5.00 |

Do not hardcode this table as the source of truth: keep it in a versioned
`pricing.yaml` and allow overrides. Models and prices change.

"Versioned" means **superseded tables are retained, not replaced**. Computing
cost at ingest only protects history if the price that was in effect can still
be found later: replay re-ingests, and a rebuild that reaches for the current
table rewrites the cost of every past run. Each `usage_event` row therefore
stores the `price_table_version` it was priced with, and replay prices with that
version or refuses (`docs/architecture.md` §4).

A model absent from the table yields `cost_usd = null`, never `0.0`. Zero is a
number someone will trust.

**Important UI label:** when Claude Code runs under a Max/Pro subscription, there
is no per-token billing. The dashboard must say "estimated equivalent cost", not
"spend". Otherwise you produce a number that alarms without meaning anything.

---

## 5. Core data model

```
Session ──1:N── Node ──1:N── Run ──1:N── Event
   │              │  └─1:N── NodeDependency (the edges)
   │              │
   └─ status: planning | running | paused | done | failed
                  └─ status: pending | ready | running |
                             awaiting_review | blocked | done | failed | skipped
```

**There is no `Graph` entity.** It would sit 1:1 with `Session`, carry no column
of its own, and add a join to the query the scheduler runs most often. The
session row *is* the graph; the edges are their own table because the scheduler
queries them on every transition and a JSON column cannot be constrained.

- **Session** — one planning conversation plus one graph. This is what appears in
  the Dashboard tab (active only) and the Sessions tab (all).
- **Node** — one activity in the graph. Carries `harness`, `model`, `prompt`,
  `acceptance_criteria`, `worktree_path`, `branch`, `depends_on: [node_id]`.
- **Run** — one *execution* of a node. A retry creates a new Run; the Node
  persists. This gives you attempt history without polluting the graph.
- **Event** — append-only. Written as NDJSON per run on disk
  (`runs/<run_id>/events.ndjson`), with only an index kept in SQLite. Long sessions
  produce tens of MB of events — not a job for a relational table, and NDJSON gives
  you trivial replay.

SQLite with WAL (`PRAGMA journal_mode=WAL`) is sufficient and correct here.
Postgres is overkill for a local single-user tool.

---

## 6. Tech stack

| Layer | Choice | Rationale |
|---|---|---|
| Backend | **FastAPI + asyncio** | Async subprocess and WebSocket handling is the core workload |
| Persistence | **SQLite WAL + SQLModel + Alembic** | Zero infrastructure, transactional |
| Events | **NDJSON on disk + SQLite index** | Replay without bloating the DB |
| Sandbox | **ai-jail** (+ git worktree) | §2 |
| Text search | **ripgrep** | Unbeatable |
| Structural search | **ast-grep** (`sg`) + tree-sitter `tags.scm` | ast-grep already solves structural matching; `tags.scm` already gives defs/refs per language — it's what GitHub uses |
| Embeddings | **sqlite-vec** | Same SQLite file, one dependency fewer. LanceDB only past ~1M chunks |
| Frontend | **Vite + React + TypeScript** | §6.1 |
| Graph | **`@xyflow/react` + ELK.js** | ELK gives decent layered DAG layout |
| Terminal | **xterm.js** + `@xterm/addon-fit` | Channel B of §3 |
| Design system | **shadcn/ui on Base UI + Tailwind**, Tremor for charts | `docs/design-system.md` |
| Metrics | **psutil** | Host CPU/mem plus per-agent process tree |
| Shared types | **openapi-typescript** over the FastAPI schema | Eliminates type drift between Python and TS |

### 6.1 Rejected alternatives

**Streamlit.** Excellent for data dashboards; it fights every requirement here.
An interactive graph with drag, zoom and node selection needs a custom React
component (you write React either way, but with a bridge layer in between). Same
for a live terminal. WebSocket-driven partial updates conflict with Streamlit's
re-run-the-whole-script model. And routing (`/sessions/:id` when clicking a
session in the dashboard) is poorly supported.

**Next.js.** There is no SSR, no SEO, no edge in this product — it is a local app
served as static files by FastAPI itself. Next.js only adds build complexity.

**Naive RAG for code search.** See §8, Tab 3.

**LanceDB.** Correct at scale, unnecessary here. sqlite-vec keeps vectors in the
same file as everything else.

---

## 7. MVP scope

**Out of the MVP:**

- **Playwright + VLM visual testing.** This is a separate product. It appears in
  none of the three tabs, is a prerequisite for nothing, and has the most moving
  parts (Chromium container, capture, image upload, vision model, pass/fail
  criteria). If an agent needs a screenshot, it can already take one via bash
  inside its worktree — no infrastructure needed on day one.
- Multi-user, auth, remote deployment, multiple simultaneous target repositories.

Bind to `127.0.0.1` and move on.

---

## 8. The three tabs

### Tab 1 — Dashboard (overview, active work only)

**KPI strip** (selectable period: today / 7d / 30d)

- Total tokens, broken down by `model` and by `harness` (stacked bars)
- Estimated equivalent cost (with the label from §4)
- Active sessions / running nodes / blocked nodes
- Node completion rate

**Active sessions** — a clickable list. Clicking navigates to `/sessions/:id`
(the same view as Tab 2). Each row: title, progress (`7/12 nodes`), harnesses in
use, accumulated tokens, elapsed time, a badge if any node is `blocked`.

**System health**

- Total and per-core CPU, used/free RAM, swap, disk (the worktree directory grows
  fast)
- Agent process table: `node_id`, PID, harness, RSS, %CPU, uptime — via
  `psutil.Process(pid).children(recursive=True)`, summing the tree
- Sampled every 1–2 s, pushed over WebSocket. Keep a ring buffer in memory (last
  ~300 points) and persist only 1-minute aggregates. Do not store 1 s metrics in
  SQLite forever.

**Event feed** — the last N meaningful transitions (node completed, node failed,
permission pending), each deep-linked.

### Tab 2 — Sessions / Orchestrator

Three-column layout:

```
┌──────────────┬───────────────────────────────┬──────────────────┐
│ Sessions     │ Chat                          │ Graph (minimap)  │
│ ─ Active     │                               │   [⤢ expand]     │
│   · sess A   │  [planning, or node chat]     │   ○──○──○        │
│   · sess B   │                               │    └──○          │
│ ─ Completed  │                               │                  │
│   · sess C   │  ┌─────────────────────────┐  │                  │
│   · sess D   │  │ input                   │  │                  │
└──────────────┴───────────────────────────────┴──────────────────┘
```

The difference from the Dashboard: **all** sessions appear here (active and
completed); the Dashboard shows only active ones.

**Planning → graph flow:**

1. You describe the goal in the chat.
2. The planner emits a DAG using **structured output** (JSON Schema) — not
   markdown parsing.

   **The planner calls the Anthropic API directly; it does not go through a
   harness.** Reusing an already-authenticated `claude -p` would avoid a
   dependency and a credential, and it is the wrong call: the CLI's
   `--output-format stream-json` structures the *event envelope*, not the
   assistant's content. There is no CLI equivalent of `output_config.format`,
   so routing the planner through a harness means prompting for JSON and
   parsing prose — exactly what this step rules out. `messages.parse()` with a
   Pydantic model gives a schema-validated object instead.

   Two consequences worth stating plainly:

   - **The planner is the one component with real per-token billing.**
     Invariant 7's "estimated equivalent cost" exists because the harnesses run
     under a subscription. The planner does not: its tokens are spend. It needs
     its own credential — an `ANTHROPIC_API_KEY`, or an `ant auth login`
     profile, which a bare client picks up with no environment variable.
   - **A valid schema is not a valid DAG.** Structured output guarantees
     well-formed JSON with the right fields; it cannot express "no cycles". The
     pure DAG core still validates, and the correction loop below is still
     required.

   Per-node schema:

```json
{
  "id": "auth_api",
  "title": "Implement authentication endpoints",
  "description": "…",
  "depends_on": ["db_schema"],
  "acceptance_criteria": ["pytest tests/test_auth.py passes", "..."],
  "suggested_harness": "claude-code",
  "suggested_model": "claude-opus-5",
  "estimated_effort": "medium",
  "touches": ["backend/auth/**"]
}
```

Four things about that schema that only became clear once the tables existed:

- **`id` here is a planner-local slug, and `depends_on` refers to slugs** — not
  to our `node_<ULID>` ids. Mapping slug to id is the planner's job and happens
  **entirely before persistence**, which is also why "orphan `depends_on`" is a
  pre-persistence concept: once rows exist, foreign keys make it unreachable.
- **`suggested_harness` / `suggested_model` are stored as `harness` / `model`.**
  The suggestion is not retained once a human overrides it — a proposal the
  operator has already answered is not worth a column.
- **`estimated_effort` has no closed vocabulary and nothing may schedule on
  it.** It is an advisory badge. An LLM's guess at effort is not a priority.
- **`acceptance_criteria` is an array, and the current column is a single
  string.** That mismatch has to be closed before the planner writes to it, or
  the criteria get joined with newlines and the per-criterion results the
  `awaiting_review` panel promises become unrecoverable. See below.

3. **Validate the DAG before rendering it**: no cycles, no orphan `depends_on`,
   topological sort possible. LLMs get this wrong regularly — on a cycle, hand the
   error back to the planner to fix instead of breaking the UI.
4. The graph appears as an **editable proposal**. You can rename, remove, add an
   edge, and — crucially — **assign a harness and a model per node**. Nothing
   executes until you approve. Human-in-the-loop at the plan stage is what
   separates this from "an autonomous agent doing damage in parallel".

**Node panel (drawer on click)** — contents depend on state:

| State | Shows |
|---|---|
| `pending` / `ready` | Edit prompt, harness, model, acceptance criteria, dependencies. "Run now" button |
| `running` | Live stream (Channel A rendered as an event feed), input to message the agent, "Attach terminal" toggle (Channel B, xterm.js), Interrupt / Kill buttons |
| `awaiting_review` | Worktree diff, acceptance-criteria results, Approve (merge) / Reject (retry with feedback) |
| `blocked` | Reason: merge conflict, failed dependency, pending permission |
| `done` / `failed` | Full transcript, diff, node tokens/cost, link to the commit, Re-run button |

Node visual encoding: color by state, badge with the harness icon, progress ring
while running. Never color alone — pair it with an icon (`docs/design-system.md` §5).

### Tab 3 — Code Search

A chat, but with an important architectural choice: **do not do naive RAG**
(embed everything → top-k → dump into the prompt). For code, that works poorly.

Do **agentic search** instead: an agent loop whose *tools* are your search engine.
The model decides what to look for, reads what it needs, and iterates.

```python
tools = [
    search_text(pattern, glob, case_sensitive),   # ripgrep
    search_structural(pattern, lang),             # ast-grep
    find_symbol(name, kind),                      # tree-sitter tags index
    find_references(symbol),
    read_file(path, start_line, end_line),
    list_directory(path),
    semantic_search(query, k),                    # sqlite-vec — last resort
]
```

This answers both "where is the tax ID validated?" and "what's the discount rule
for recurring customers?", because the agent navigates instead of depending on a
single similarity guess.

UI requirement: every claim must cite a clickable `path/to/file.py:123`, with a
side panel showing the snippet with syntax highlighting.

Indexing: `watchfiles` watching the repo → incrementally reindex only what
changed. Never reindex everything in the foreground.

---

## 9. Graph scheduler

Do not build a generic workflow engine. A topological scheduler over asyncio is
enough:

```python
async def run_graph(graph, max_concurrency=3):
    sem = asyncio.Semaphore(max_concurrency)
    done: set[str] = set()

    async def run_node(node):
        async with sem:
            await materialize_worktree(node)   # base = merge of parents
            await execute(node)                # harness adapter
            await check_acceptance(node)
            # → awaiting_review (if the human gate is on) or automatic merge

    while not graph.is_complete():
        ready = [n for n in graph.nodes
                 if n.status in ("pending", "ready")
                 and set(n.depends_on) <= done]
        if not ready and not running:
            break   # see the four outcomes below
        ...
```

Two corrections to that sketch, both found by building it:

**A startable node is `pending` *or* `ready`.** State is persisted on every
transition, so a node marked `ready` and not yet launched when the orchestrator
dies would never be picked up again after a restart.

**`not ready and not running` is three different situations, not one.** The
scheduler distinguishes four outcomes:

| Outcome | Meaning |
|---|---|
| `active` | Something is running or startable. |
| `waiting_on_human` | A gate holds it — `awaiting_review`, or a `blocked` node needing resolution. Under `auto_merge` off this is the system working correctly (invariant 6), not a stall. |
| `complete` | Every node reached a terminal state. |
| `deadlocked` | Nothing running, nothing ready, no gate open, not complete. |

Given a validated DAG, `deadlocked` is only reachable when a transition was not
persisted — so it detects a scheduler bug, not a graph state, and deserves a
loud log rather than a quiet exit.

**A `skipped` node satisfies its dependents.** A skip that blocks everything
downstream is not a usable operator action.

**`check_acceptance` does not evaluate the criteria — a human does.** §8 emits
them as prose (`"pytest tests/test_auth.py passes"` describes a command, it is
not one), and there is no honest way to run prose. Guessing which strings are
shell commands would be a heuristic that silently passes a criterion it failed
to understand, which is worse than not checking. So the run records each
criterion against its outcome and the review panel presents them as a checklist
the reviewer resolves.

With `auto_merge` on there is no reviewer, and the criteria are recorded but not
enforced. That is a real limitation and it is stated rather than hidden: an
unattended graph merges on the harness's own verdict.

The upgrade path, when it is worth building, is to give a criterion an optional
`command` in §8's schema — then the ones that *are* checkable run in the node's
worktree under the sandbox, and the rest still go to the human. That is a
planner-schema change and belongs with whoever next touches it.

What matters:

- **`max_concurrency` configurable and low by default (2–3).** Each agent is a
  heavy process and consumes rate limit. Ten in parallel will lock up the machine
  and blow through API limits.
- **State persisted on every transition.** The orchestrator must be able to
  restart and either re-find running nodes (PIDs on disk) or mark them orphaned.
- **Per-node timeout and budget** (tokens and wall-clock). An agent in a loop burns
  hundreds of thousands of tokens silently. Cut it off at the limit and mark it
  `failed`.
- **Configurable human gate:** `auto_merge` vs `awaiting_review`, per session.

---

## 10. Phases

The unknowns are in harness + PTY + worktree. If something is going to kill this
project, it is there — and you want to find out in week 1, not week 4. That is why
code search, the easiest and lowest-risk piece, comes last.

### Phase 0 — Vertical spike (1–2 days) ⟵ start here

A Python script, no UI, that:

1. Creates a worktree from the target repo
2. Launches `ai-jail claude -p --output-format stream-json --verbose` inside it
3. Parses the events and prints text, tool calls, and **accumulated tokens**
4. Commits the result and merges into the integration branch

If this works, the whole project is viable. If it doesn't, you find out now which
real harness flags and behaviors diverge from the documentation.

### Phase 1 — Single-node orchestrator

FastAPI + SQLite + WebSocket. One session, one node, live streaming in the browser
(structured feed + xterm.js). Kill/retry working. Includes `agenthub replay <run_id>`
(see `docs/architecture.md` §4). No graph yet.

### Phase 2 — Graph

Planner with structured output → DAG validation → editable React Flow →
scheduler with concurrency → per-worktree merge. **This is the heart of the
product.**

### Phase 3 — Dashboards

Token/cost KPIs, system metrics via psutil, active session list with deep links.
By now you have real data to display — building dashboards before having data is
waste.

### Phase 4 — Code Search

ripgrep + ast-grep + tree-sitter tags + agentic chat. Then sqlite-vec.

### Post-MVP

Playwright + VLM, multi-repo, remote execution, permission approval through the UI.

---

## 11. Directory layout

```
agenthub/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI, serves the static SPA
│   │   ├── api/                 # REST routes
│   │   ├── ws/                  # WebSocket: events, PTY, metrics
│   │   ├── models/              # SQLModel + Alembic
│   │   ├── orchestrator/
│   │   │   ├── graph.py         # pure DAG logic
│   │   │   ├── scheduler.py     # topological DAG + asyncio
│   │   │   ├── planner.py       # LLM → DAG (structured output)
│   │   │   └── worktree.py      # git worktree lifecycle
│   │   ├── harnesses/
│   │   │   ├── base.py          # BaseHarnessAdapter
│   │   │   ├── events.py        # AgentEvent
│   │   │   ├── claude_code.py
│   │   │   ├── codex.py
│   │   │   └── opencode.py
│   │   ├── sandbox/
│   │   │   └── aijail.py        # ai-jail argv construction
│   │   ├── storage/             # NDJSON, SQLite, replay
│   │   ├── search/
│   │   │   ├── ripgrep.py
│   │   │   ├── astgrep.py
│   │   │   ├── symbols.py       # tree-sitter tags.scm
│   │   │   └── vectors.py       # sqlite-vec
│   │   └── metrics/
│   │       └── system.py        # psutil
│   ├── tests/fixtures/          # recorded harness output (golden tests)
│   └── pyproject.toml
├── frontend/                    # Vite + React + TS
│   ├── src/
│   │   ├── routes/              # /dashboard  /sessions/:id  /search
│   │   ├── components/graph/    # @xyflow/react + ELK
│   │   ├── components/terminal/ # xterm.js
│   │   ├── styles/tokens.css
│   │   └── api/                 # types generated by openapi-typescript
│   └── package.json
├── docs/
├── pricing.yaml                 # versioned pricing table
└── README.md
```

There is no `docker-compose.yml` in the MVP — there is no service to orchestrate.
It comes back if and when Playwright does.

---

## 12. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Harness flags change between versions | High | Phase 0 validates early; adapters isolate the rest of the system; per-harness contract tests |
| Merge conflicts between parallel nodes | High | The graph should minimize file overlap (`touches` field in the node schema); a conflict becomes `blocked`, not a crash |
| Planner produces an invalid DAG | Medium | Topological validation plus a correction loop before rendering |
| Runaway agent burns tokens | Medium | Per-node token budget and timeout, with kill |
| Wrong token accounting | Medium | Sum all four fields (§4); test against known values |
| Disk blowup from worktrees | Low | GC completed sessions; alert on the dashboard disk card |
| `sandbox-exec` is deprecated on macOS | Low | That is ai-jail's problem, not yours; the `sandbox/aijail.py` abstraction allows swapping it |
