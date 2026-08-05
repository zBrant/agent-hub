# Roadmap

Progress tracking. The rationale for this ordering and the full description of
each phase live in [`design.md`](../design.md) §10 — this file only records
**where the project is**.

The unknowns are in the harness, PTY, and worktree layers. If something kills
this project, it's there — so that comes first, and code search, the easiest and
lowest-risk piece, comes last.

## Status

**Pre-implementation.** The repository currently contains only design and
architecture documents. No code has been written.

## Phases

- [ ] **Phase 0 — Vertical spike.** No UI. A script that creates a worktree,
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
