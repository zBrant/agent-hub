# Roadmap

Progress tracking. The rationale for this ordering and the full description of
each phase live in [`design.md`](../design.md) §10 — this file only records
**where the project is**.

The unknowns are in the harness, PTY, and worktree layers. If something kills
this project, it's there — so that comes first, and code search, the easiest and
lowest-risk piece, comes last.

## Status

**Phase 1 implementation.** Phase 0 is complete. Phase 1 activities B1–B4 are
complete: local-only FastAPI, migrated SQLite projections, ordered
NDJSON→SQLite→broadcast ingest with deterministic replay, and the persistent
single-node run service. The B8 frontend shell is also in place; generated types
remain gated on B5. The local suite has 360 passing tests (1 harness skip), and
all static architecture and frontend type gates pass. REST (B5), WebSocket (B6),
and kill/retry (B7) are next and can now proceed from the service boundary. See
[`phase-1.md`](phase-1.md) for the activity details.

## Phases

- [x] **Phase 0 — Vertical spike.** No UI. A script that creates a worktree,
      launches a sandboxed harness, parses events and accumulated tokens, then
      commits and merges. If this works, the product is viable.
      Activity breakdown: [`phase-0.md`](phase-0.md).
- [ ] **Phase 1 — Single-node orchestrator.** FastAPI + SQLite + WebSocket, one
      session and one node streaming live in the browser, kill/retry,
      `agenthub replay <run_id>`. No graph yet.
- [ ] **Phase 2 — The graph.** Planner with structured output, DAG validation,
      editable canvas, concurrent scheduler, per-worktree merge. *The heart of
      the product.*
- [ ] **Phase 3 — Dashboards.** Token/cost KPIs and system metrics, once there is
      real data to show.
- [ ] **Phase 4 — Code search.** ripgrep + ast-grep + tree-sitter tags + agentic
      chat, then sqlite-vec.

Install and run instructions land with Phase 1, and the README gets them then.

## Post-MVP

Visual testing with Playwright and a vision model, multi-repo, remote execution,
permission approval through the UI.
