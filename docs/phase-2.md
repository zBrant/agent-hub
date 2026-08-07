# Phase 2 — The graph

**Goal:** a described objective becomes a validated DAG of activities, a human
edits and approves it, and a concurrent scheduler executes it — each node in its
own worktree, each merging into the session integration branch.

This is the heart of the product (`design.md` §10). Phases 0 and 1 proved one
node can run, be observed, and be replayed. Phase 2 is where the graph stops
being a picture next to a task list and becomes the unit of execution.

## What is actually risky here

Not the planner. Structured output against a JSON Schema is well-understood, and
`design.md` §8 already specifies the node schema and the "hand a cycle back to
the planner" loop.

The risk is that **`SingleRunService` is built on "one active run per session"**,
and generalizing it touches everything at once: the ingest path assumes one
`meta.json` being finalized, the broker assumes one run topic per session, kill
and retry assume one `_ActiveRun`, and the worktree base stops being "the
integration branch" and becomes "the merge of the parents". Concurrency
multiplies every failure mode Phase 1 handled once.

Second risk, close behind: **parallel merges into one integration branch**.
Phase 1 merged one node with nothing else in flight. With three nodes finishing
within seconds of each other, the integration worktree is a single shared
resource, and `docs/architecture.md` §4's write ordering says nothing about it.

So the ordering below front-loads both, and leaves the planner — the visible,
demo-friendly part — until the machinery underneath it is real.

## Activities

```mermaid
flowchart TD
    C1[C1 · Graph schema] --> C2[C2 · Pure DAG core]
    C1 --> C4[C4 · Multi-node worktrees]
    C2 --> C3[C3 · Scheduler]
    C4 --> C3
    C3 --> C5[C5 · Merge serialization]
    C3 --> C6[C6 · Budgets and restart recovery]
    C5 --> C7[C7 · Human gate]
    C6 --> C7
    C7 --> C8[C8 · Planner]
    C8 --> C9[C9 · Graph REST and WS]
    C9 --> C10[C10 · Editable canvas]
    C10 --> C11[C11 · Node drawer]
    C11 --> C12[C12 · Acceptance]
```

---

### C1 — Graph schema and migration

A `Session` today owns exactly one `Node`. It must own many, with edges.

Add `depends_on`, `touches`, `acceptance_criteria`, and per-node `harness` and
`model` (`design.md` §8's planner schema). Add `auto_merge` at the session level
if B2 did not already land it.

Edges are their own table, not a JSON column on `node`: the scheduler queries
them, and a JSON blob makes "which nodes are ready" a full scan plus a parse.

Keep B2's discipline — authored versus derived columns, and every derived one
still rebuildable from `events.ndjson` (invariant 4). `depends_on` is authored:
no log can invent it.

**Done when:** Alembic migrates an existing Phase 1 database forward without
losing its single node, and repository tests cover a multi-node session with
edges, including a self-edge and a duplicate edge being rejected at the
database level.

---

### C2 — Pure DAG core

`orchestrator/graph.py`, extending what is there rather than replacing it.

Cycle detection, orphan `depends_on` detection, topological sort, ready-set
computation given a set of completed nodes, and deadlock detection — nothing
ready, nothing running, not complete.

**Pure. No I/O, no async, no database** (`docs/architecture.md` §3). This is the
module that should have exhaustive tests, because it is the only part of the
scheduler that can be tested without processes, worktrees, or time.

**Done when:** property-style tests cover cycles of length 1, 2 and n, diamonds,
disconnected components, and the deadlock case; and every function is total —
an invalid graph returns a typed error, never raises.

---

### C3 — Concurrent scheduler

`orchestrator/scheduler.py`. The topological loop from `design.md` §9 — and
nothing more. **Do not build a generic workflow engine.**

`max_concurrency` configurable, **default 2**. Each agent is a heavy process
holding a rate limit; ten in parallel locks the machine and exhausts the quota.

State is persisted on **every** transition, not at the end. The scheduler must be
able to die at any point and have the database describe reality.

This is where `SingleRunService` generalizes. Decide deliberately whether it
becomes `GraphService` or grows a scheduler alongside it — but the harness-name
neutrality (invariant 1) and the NDJSON→SQLite→broadcast ordering
(`docs/architecture.md` §4) are not negotiable, and both currently assume one
run at a time.

**Done when:** a fake adapter drives a diamond graph to completion with
`max_concurrency=2`, provably never exceeding two concurrent runs; and a node
failure leaves its dependents `blocked` rather than `pending` forever.

---

### C4 — Multi-node worktree materialization

`design.md` §2.2 and the Phase 0 A7 findings: `git worktree add` takes **one**
commit-ish, so a multi-parent node is created off the first parent with the
remaining parents merged in, and folding stops at the first conflict — the node
comes back `blocked` before any agent launches.

`worktree.py` already implements this (`create_node(parents=...)`). C4 is about
driving it from the graph and proving it under concurrency, not rewriting it.

**Done when:** a diamond graph produces a final node whose worktree contains the
edits of both parents, and a deliberate parent conflict blocks the child without
launching an agent.

---

### C5 — Merge serialization

Three nodes finishing at once contend for one integration worktree. Git will not
protect you: a second `git merge` during an unfinished one fails in a way that
looks like a conflict.

Serialize integration merges explicitly. The lock is on the *integration
worktree*, not on the session — node worktrees stay parallel, only the merge is
sequential, and it is fast.

A conflicted merge is aborted, not left in place (`design.md` §2.2) — leaving the
shared worktree in `MERGING` blocks every other node behind one human, which is
exactly the failure this activity exists to prevent.

**Done when:** a test races N nodes into integration and every one of them lands
or blocks, with no interleaved merge state and no lost commit.

---

### C6 — Budgets, timeouts and restart recovery

Per-node **token budget and wall-clock timeout**, with kill (`design.md` §9 and
§12's runaway-agent risk). An agent in a loop burns hundreds of thousands of
tokens silently; the cutoff marks the node `failed`.

Phase 0 A3 found that a budget-exhausted Claude Code turn reports
`result.usage` as all zeros, so the budget check must read the reconstructed
`Usage` — the one B3 marks `source="reconstructed"` — not the raw one.

Restart recovery: on startup, every `run` row still `RUNNING` is an orphan. B2's
`RunState` docstring already says the scheduler resolves it to `INTERRUPTED`
rather than inventing a sixth state. Either re-find the process by PID or mark
it orphaned; do not leave the row lying.

**Done when:** a token budget kills a running node mid-stream and the node is
`failed` with its partial usage recorded; and a scheduler restarted with a
`RUNNING` row resolves it without human intervention.

---

### C7 — Human gate

`auto_merge` off means a finished node stops at `awaiting_review` (invariant 6:
the planner's graph is a proposal, and nothing runs — or merges — before
approval).

Approve merges; reject retries **with feedback**, which means the rejection text
reaches the next run's prompt. B7's immutable-attempt rule holds: a retry is a
new `Run`, never a mutated one.

**Done when:** with `auto_merge` off, a completed node blocks its dependents
until approved; and a rejection's feedback text is present in the retry's
`meta.json` argv or prompt.

---

### C8 — Planner

`orchestrator/planner.py`. Objective → DAG via **structured output against a
JSON Schema**, never markdown parsing. The node schema is specified in
`design.md` §8.

Validate before rendering. On a cycle or an orphan `depends_on`, **hand the
error back to the planner to fix** — LLMs get this wrong regularly, and breaking
the UI over it is a choice, not a necessity. Bound the correction loop; a
planner that cannot produce a valid DAG in three attempts is a failure to report,
not a loop to run forever.

The proposal is persisted as a graph in `pending` state. Invariant 6: nothing
executes before human approval while `auto_merge` is off.

**Done when:** a recorded planner response builds a valid graph; an injected
cyclic response triggers exactly one correction round-trip and then succeeds; and
an incorrigible planner fails with a message naming the cycle.

---

### C9 — Graph REST and WebSocket

Create a graph from a planner proposal, edit nodes and edges before approval,
approve, run, and per-node operations. Routes validate and delegate; every
decision stays in the orchestrator (`docs/architecture.md` §1 rule 3).

The WebSocket gains a graph-level topic carrying node status transitions. Reuse
B6's bounded, cursor-replay broker — do not write a second one.

**Done when:** API tests cover editing a graph before approval, refusing to edit
one after it has started, and a reconnect replaying node transitions without a
gap.

---

### C10 — Editable canvas

`@xyflow/react` + `elkjs` for layout, per `design.md` §6 and §8. The graph is an
**editable proposal**: rename, remove, add an edge, and assign harness and model
per node.

Node visual encoding comes from `docs/design-system.md` §5 — colour by state
always paired with an icon, never colour alone. `src/lib/node-state.ts` already
holds that mapping from B8; consume it rather than redefining it.

**Done when:** a proposal renders, survives edit and reload, and the approve
action is disabled while the client-side graph is invalid.

---

### C11 — Node drawer

The per-state panel from `design.md` §8's table: edit when `pending`/`ready`,
live stream and message input when `running`, diff and approve/reject when
`awaiting_review`, reason when `blocked`, transcript and re-run when
`done`/`failed`.

Most of this exists in B9's single-session view. C11 is largely about making it
addressable per node rather than per session.

**Done when:** clicking any node in a running graph opens its live feed, and the
`awaiting_review` state offers a diff and both actions.

---

### C12 — Acceptance

One real multi-node graph, planned from an objective, edited by hand, approved,
executed with concurrency against a real repository, with at least one
deliberate conflict and one approval gate. Replay every run from NDJSON and
verify the database, REST and UI agree on the totals.

**Done when:** the acceptance record is committed and the roadmap marks Phase 2
complete.

## Explicitly out of scope

Dashboards and system metrics (Phase 3), code search (Phase 4), OpenCode,
multi-repo, remote execution, and the resolver agent for conflicted merges — a
conflict blocks and waits for a human in this phase.
