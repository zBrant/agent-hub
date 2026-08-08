# Code architecture

`design.md` decides **what** AgentHub does. This document decides **where each
thing lives** and **who may call whom**. It is the contract that keeps the system
changeable after Phase 2.

---

## 1. Dependency direction

```
                    ┌──────────────────────────────┐
                    │  frontend/ (Vite + React)    │
                    └──────────────┬───────────────┘
                          REST + WebSocket
                    ┌──────────────▼───────────────┐
   entry            │  api/          ws/           │   ← transport only
                    └──────────────┬───────────────┘
                    ┌──────────────▼───────────────┐
   decision         │  orchestrator/               │   ← scheduler, planner, worktree
                    └──────┬───────────────┬───────┘
                    ┌──────────────▼───────────────┐
   persistence      │  storage/                   │   ← event log, projections
                    └──────────────┬───────────────┘
                    ┌──────▼──────┐ ┌──────▼───────┐
   execution        │ harnesses/  │ │  sandbox/    │
                    └──────┬──────┘ └──────────────┘
                    ┌──────▼───────────────────────┐
   data             │  models/                    │
                    └──────────────────────────────┘

   independent verticals:  search/     metrics/
```

Rules, in order of importance:

1. **Arrows only point down.** `harnesses/` does not import `orchestrator/`.
   `orchestrator/` does not import `api/`. `models/` imports nothing from the app.
   `storage/` sits above execution because an event log must import the canonical
   `AgentEvent` union from `harnesses/events.py`; it never imports a concrete
   harness adapter.
2. **`search/` and `metrics/` are isolated verticals.** They do not import
   `orchestrator/` or `harnesses/`. Either could be extracted into a separate
   process without touching the rest.
3. **`api/` and `ws/` contain no logic.** They validate input, call an
   `orchestrator/` use case, and serialize the output. A business-rule `if` inside
   a route is in the wrong place.
4. **All disk and git writes go through `orchestrator/worktree.py` or
   `storage/`.** No other module shells out to `git` or opens a run file.

How to enforce it: `import-linter` with a layers contract in `pyproject.toml`,
running alongside the tests. It is cheap and catches architectural regressions
that human review lets through.

---

## 2. The boundary that holds everything up: `AgentEvent`

The whole system exists to turn *N different CLIs* into *one uniform stream*. That
translation happens in exactly one place.

```
claude -p --output-format stream-json ─┐
codex exec --json ─────────────────────┼──► harnesses/*.py ──► AgentEvent ──► everything else
opencode serve (SSE) ──────────────────┘
```

**Rule:** outside `backend/app/harnesses/`, nobody knows which CLI is running.
`harness` and `model` are *data* that appear in events and flow to the dashboard —
they are not behavioral conditionals.

Smell test: grep for `== "claude-code"`, `startswith("codex")`, `harness in (...)`.
Outside `harnesses/` and the model catalog, every hit is a leak.

Practical consequence: when one harness exposes something the others don't, you
have two legitimate options — **generalize** the event (with an optional field) or
**drop** the information. The illegitimate option is branching downstream.

`AgentEvent` is a Pydantic discriminated union (`Field(discriminator="type")`)
defined in `harnesses/events.py`. It is serialized to three destinations using
**the same** serialization:

- a line in `events.ndjson`
- a WebSocket frame
- a replay payload

One serialization, three uses. Do not create a separate DTO for the WebSocket.

---

## 3. Pure core, imperative shell

The scheduler is the part most likely to hide subtle bugs (concurrency +
persisted state + retry). Separate decision from effect:

```python
# orchestrator/graph.py — pure, no I/O, not async
def ready_nodes(graph: Graph, done: set[NodeId], running: set[NodeId]) -> list[Node]: ...
def validate_dag(graph: Graph) -> list[DagError]: ...        # cycles, orphans, topo-sort
def transition(node: Node, event: AgentEvent) -> NodeStatus: ...

# orchestrator/scheduler.py — impure, async, calls the above
async def run_graph(graph: Graph, *, max_concurrency: int = 3) -> None: ...
```

What this buys: the DAG logic — the part an LLM planner will stress with strange
input — is testable with plain dictionaries, no subprocess, no git, no database.
Tests run in milliseconds and you can write fifty of them.

**State transitions live in one place.** There is no `node.status = "failed"`
scattered around. There is `transition()`, and the scheduler applies its result.
Node status has eight values (`design.md` §5); scattering transitions guarantees
one of them becomes unreachable.

---

## 4. Persistence: NDJSON primary, SQLite derived

```
~/.agenthub/
├── agenthub.db              # SQLite WAL — sessions, graphs, nodes, runs, usage_event
├── runs/
│   └── <run_id>/
│       ├── events.ndjson    # append-only, source of truth
│       ├── meta.json        # see below — what the log cannot carry
│       └── pty.log          # raw Channel B bytes (optional, rotated)
└── workspaces/
    └── <session_id>/{integration,node_a,node_b}/
```

Write path for an event, in this order:

1. append to `events.ndjson` and flush the record
2. update the SQLite projection (`run` state and append-only usage rows)
3. broadcast on the WebSocket

An operator kill follows the same path. The adapter terminates its isolated
process group and synthesizes `RunFinished(status="interrupted")`; that event is
appended, projected, and broadcast like any other terminal fact. Partial work
is checkpointed but never merged. Retry inserts another `run` row and another
NDJSON directory for the same node — it never edits the previous attempt or
reuses its log.

If the process dies between 1 and 2, replay reconstructs. Reverse the order and
you have state in the database that does not exist in the log — and you lose the
ability to audit.

The live writer flushes every record from Python's buffer. It does not call
`fsync()` per line: the recovery model is process death, not sudden power loss,
and a disk sync for every streamed event would stall throughput.

**Required command:** `agenthub replay <run_id>` rebuilds the projections from the
NDJSON. If it does not exist, or does not match the database, invariant 4 in
`AGENTS.md` is fiction.

`usage_event` is append-only and never `UPDATE`d. Dashboard aggregates are `SUM()`
over an index, not a mutable counter.

### `meta.json` — what the event log structurally cannot carry

An `AgentEvent` describes what the *agent* did. Three things a rebuild needs are
facts about the *orchestration*, and no harness will ever emit them:

| Field | Why the log cannot supply it |
|---|---|
| `session_id`, `node_id`, `attempt` | `RunStarted` carries the **harness's** session id, not ours. Without these, a run row deleted for rebuild cannot be relinked to its node or retain its attempt number. |
| `price_table_version` | See below. |
| `argv`, `cwd`, sanitized `env`, harness version | The launch conditions. Reproducing a run means reproducing these. |
| parser trust (`unknown`, `malformed`, unreconciled usage) | `ParseStats` is adapter state, not an event. B7 must refuse to merge a parser-untrusted run and B9 must show it, so it has to be durable somewhere. |
| `created_ms` | A rebuild must retain the original row timestamp rather than stamp the replay time. |

`meta.json` is written once at run start and finalized at run end. It is part of
the source of truth, not a projection: deleting it loses information that
`events.ndjson` cannot reconstruct.

### Replay must not reprice history

Invariant 3 says `cost_usd` is computed at ingest **with the price in effect at
that moment**. Replay re-ingests, so a naive rebuild recomputes old runs at
today's prices and silently rewrites cost history — which is the exact failure
the invariant exists to prevent.

The rule: `meta.json` pins the `price_table_version` used at ingest, every
`usage_event` row carries it, and replay prices with **that** version. This
requires `pricing.yaml` to retain superseded tables rather than only the current
one.

If the pinned version is absent, replay must permanently **refuse and say so**.
That remains true even after superseded tables are retained: a table can be
deleted accidentally or the database can move beside an older `pricing.yaml`.
Refusing is recoverable; silently repricing is not, and it is invisible in the
diff.

---

## 5. The two channels per run

| | Channel A — structural | Channel B — PTY |
|---|---|---|
| Source | `--output-format stream-json` | `os.openpty()` |
| Becomes | typed `AgentEvent` | `RawChunk(bytes)` |
| Persisted to | `events.ndjson` | `pty.log` (rotated, disposable) |
| Feeds | state, tokens, graph, dashboards, history | `xterm.js` only |
| Enabled | always | on demand, only for a proven live-attach capability |

**Never derive state from Channel B.** It is pixels, not data. Channel B may be
off for an entire session with nothing in the system noticing — if that isn't
true, someone is parsing ANSI somewhere.

Backpressure matters: Channel B produces a lot of bytes. Use
`asyncio.Queue(maxsize=N)` with a drop-from-the-middle policy, and never let a
slow WebSocket client hold back the PTY reader.

### Codex attach classification (validated 2026-08-06)

The installed Codex CLI 0.146.0 has three distinct operations that must not be
collapsed into one feature:

| Operation | Classification | Consequence |
|---|---|---|
| `thread/read` against app-server | observational | Reads persisted history and does not subscribe to live events |
| Two app-server clients using `thread/resume` | interactive | Both receive the same turn/item stream and either can drive later turns |
| `codex exec --json` followed by another process | continuation only | The second process is not attached to the active `exec` runtime |

The real-CLI experiment used one persisted Codex thread, two simultaneous
WebSocket JSON-RPC clients, and one `turn/start`. Both clients received
`turn/started`, item deltas, token usage, and `turn/completed`, including the
same final `B10_SHARED` message. The actual terminal UI then connected with
`codex resume <thread-id> --remote ws://127.0.0.1:<port>` in a PTY and rendered
the same `B10_SEED` and `B10_SHARED` history. This proves that app-server is an
interactive topology when it owns the session.

It does **not** make app-server a sidecar for the current Channel A process.
`codex exec --json` has no `--remote` option, and resuming its stored thread in
app-server creates a separately driven runtime. During the original turn that
runtime cannot faithfully mirror the active process; after the turn it is a
continuation, which B7 deliberately keeps separate from retry. Letting it edit
the node would also bypass the current run's NDJSON, checkpoint, and merge
lifecycle.

Therefore Phase 1 does not expose a Codex terminal or start a PTY reader. There
is intentionally no raw-byte/WebSocket bridge whose consumer could apply
backpressure. Channel B remains gated until the Codex adapter can use app-server
as its primary, ai-jail-contained runtime and preserve Channel A's durable
event, usage, interruption, and approval contracts. When that migration is
made, the bounded drop-from-the-middle rule above is a release gate, not an
optional optimization.

---

## 6. Frontend: three state sources, never mixed

| Source | Tool | Rule |
|---|---|---|
| Server state (sessions, graphs, history) | TanStack Query | Never copy into a local store |
| Live state (events, metrics, PTY) | **one** WebSocket connection → Zustand store | Never poll for what already arrives over WS |
| UI state (open drawer, graph zoom, filter) | `useState` / local store | Never persist to the server |

**A single WebSocket connection** for the whole app, multiplexed by topic
(`session:<id>`, `run:<id>`, `graph:<session_id>`, `metrics`). One connection
per panel overloads the backend and produces divergent event ordering between
components. Graph topics carry persisted node-status transitions; harness
events remain on session and run topics.

Event frames carry a process-scoped `stream` and a monotonic `seq` per topic.
The client reconnects with both values; the broker atomically replays its
bounded history before attaching live delivery. A cursor from another backend
process is reset with a fresh `ready` checkpoint. If a cursor predates retained
history, the broker reports `history_gap`; the consumer fetches persistent REST
state before attaching at a fresh checkpoint. Never disguise a gap as a
successful subscription.

An event arriving over WS **invalidates a query** when it's a structural change
(node completed, merge happened), and **updates the store** when it's stream data
(text delta, PTY chunk). Confusing the two causes flicker or stale state.

The live feed is hydrated from the run's persisted NDJSON reader before live
facts are reconciled into its Zustand topic store. The reader returns the same
canonical `AgentEvent` JSON serialization as the WebSocket, but its union stays
generated from Pydantic's explicit JSON Schema rather than being duplicated in
OpenAPI. This is what lets a browser refresh recover narrative events while
SQLite remains only the derived structural index.

Route components (`routes/`) are the only ones that compose — they fetch data and
pass it down. Components in `components/` receive props and know nothing about the
API.

---

## 7. Shared types (no manual drift)

```
FastAPI  ──► /openapi.json ──► openapi-typescript ──► frontend/src/api/schema.d.ts
AgentEvent ──► model_json_schema() ──► json-schema-to-typescript ──► frontend/src/api/events.d.ts
```

`AgentEvent` travels over the WebSocket, so it does not appear in the OpenAPI
schema. Export its JSON Schema explicitly from a script
(`backend/scripts/export_schemas.py`) that runs in pre-commit and fails if the
generated file is out of date.

**Hand-writing a TypeScript type that mirrors a Python model is forbidden.** The
mirror always drifts, and the drift shows up in production as an `undefined` field
in the middle of a stream.

---

## 8. Responsibility per module

| Module | Does | Does not |
|---|---|---|
| `api/` | REST routes, validation, serialization | business logic, git access |
| `ws/` | topic multiplexing, backpressure | parsing harness output |
| `orchestrator/graph.py` | pure DAG: validation, readiness, transition | any I/O |
| `orchestrator/service.py` | Phase 1 session/run lifecycle, safety gates | HTTP or harness-specific branching |
| `orchestrator/scheduler.py` | concurrency, retry, budget, transition persistence | talk to a CLI directly |
| `orchestrator/planner.py` | LLM → DAG via structured output + correction loop | execute nodes |
| `orchestrator/worktree.py` | git lifecycle: create, merge, conflict, GC | decide *when* to create |
| `harnesses/` | translate CLI ↔ `AgentEvent`, PTY, message injection | know about graphs or sessions |
| `sandbox/aijail.py` | build ai-jail argv from a policy | run processes |
| `storage/` | NDJSON, SQLite, replay | domain logic |
| `search/` | ripgrep, ast-grep, tags, vectors, agentic loop | anything orchestration-related |
| `metrics/` | psutil, ring buffer, aggregation | persist 1 s samples |

---

## 9. Errors: agent failure is data, an exception is a bug

- The agent failed, the timeout fired, the merge conflicted → **an event**
  (`RunFinished(status="failed")`, node moves to `blocked`). Normal flow.
- Invalid argument, violated invariant, unhandled union case → **an exception**.
  That is programmer error and should surface loudly.

Never use exceptions as control flow between layers. The scheduler should not wrap
`execute(node)` in `try/except` to decide a status — the adapter already reports
the status in its final event.

A bare `except Exception` is acceptable only at the edge of an `asyncio.Task`, and
always with structured logging plus an explicit node transition.

---

## 10. Testing: where to invest

| Layer | Type | Goal |
|---|---|---|
| `orchestrator/graph.py` | pure unit tests, many cases | high coverage; it's cheap |
| `harnesses/*` | **golden file**: recorded real NDJSON → parser → expected events | catches CLI flag changes |
| `harnesses/*` | **contract test** (`@pytest.mark.harness`) against the real CLI | skipped when the binary is absent |
| `worktree.py` | integration against a temporary git repo | create, merge, conflict, cleanup |
| `api/` | smoke tests via `TestClient` | do not test rules here |
| frontend | Vitest on reducers/stores; Playwright on the three main flows only | do not chase UI coverage |

Record real fixtures from Phase 0 onward: run Claude Code once, save the raw
`stream-json` into `tests/fixtures/claude_code/*.ndjson`. When the flags change in
the next release, the golden test tells you before a user does.

---

## 11. Where things will change

Points we already know will be unstable — keep the boundary clean around them:

- **Harness flags and output formats** → isolated in `harnesses/`, covered by
  golden tests.
- **Pricing table and model catalog** → `pricing.yaml`, loaded at runtime, never
  hardcoded.
- **Sandbox policy** → `sandbox/aijail.py` builds argv from a `SandboxPolicy`
  object; replacing ai-jail means rewriting one file.
- **Graph layout** → ELK.js is a component detail, not part of the data model. The
  backend never sends coordinates.
