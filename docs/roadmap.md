# Roadmap

Progress tracking. The rationale for this ordering and the full description of
each phase live in [`design.md`](../design.md) §10 — this file only records
**where the project is**.

The unknowns are in the harness, PTY, and worktree layers. If something kills
this project, it's there — so that comes first, and code search, the easiest and
lowest-risk piece, comes last.

## Status

**Phase 1 complete.** Activities B1–B11 passed: local-only FastAPI, migrated
SQLite projections, ordered NDJSON→SQLite→broadcast ingest with deterministic
replay, and the persistent
single-node run service with its persistent REST resource API and bounded,
cursor-replay WebSocket broker, process-group kill, and immutable-attempt retry.
B8/B9 are also complete: generated contracts, the persistent/live session
view, structured feed, usage/cost, diff, and lifecycle controls. B10 proved the
Codex app-server topology is interactive but not attachable to the active
`exec --json` runtime, so the misleading PTY bridge is explicitly deferred.
B11 then exercised the full path with a real Codex session over HTTP and
WebSocket, including reconnect, kill, immutable retry, two NDJSON replays, and
database/REST/UI total agreement. See [`acceptance-phase-1.md`](acceptance-phase-1.md)
for the evidence and [`phase-1.md`](phase-1.md) for the activity details.

**Phase 2 is in progress.** C1–C11 are complete: graph persistence and the pure
DAG core, concurrent worktrees and serialized merges, the scheduler with
budgets/recovery, the acceptance gate, structured planner, and the complete
graph REST/WebSocket orchestration surface, followed by the editable React Flow
canvas with client-side DAG validation and the per-state node drawer. C12 — the
real multi-node acceptance run — is next; see [`phase-2.md`](phase-2.md).

## Phases

- [x] **Phase 0 — Vertical spike.** No UI. A script that creates a worktree,
      launches a sandboxed harness, parses events and accumulated tokens, then
      commits and merges. If this works, the product is viable.
      Activity breakdown: [`phase-0.md`](phase-0.md).
- [x] **Phase 1 — Single-node orchestrator.** FastAPI + SQLite + WebSocket, one
      session and one node streaming live in the browser, kill/retry,
      `agenthub replay <run_id>`. No graph yet.
- [ ] **Phase 2 — The graph.** Planner with structured output, DAG validation,
      editable canvas, concurrent scheduler, per-worktree merge. *The heart of
      the product.* Activity breakdown: [`phase-2.md`](phase-2.md).
- [ ] **Phase 3 — Dashboards.** Token/cost KPIs and system metrics, once there is
      real data to show.
- [ ] **Phase 4 — Code search.** ripgrep + ast-grep + tree-sitter tags + agentic
      chat, then sqlite-vec.

Install and run instructions are in the repository `README.md`.

## Post-MVP

Visual testing with Playwright and a vision model, multi-repo, remote execution,
permission approval through the UI.
