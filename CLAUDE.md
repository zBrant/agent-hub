# AgentHub

Local orchestrator for AI coding agents: a dependency graph of activities,
multiple harnesses (Claude Code, Codex, OpenCode), code search, and token/system
dashboards. Single-user, binds to `127.0.0.1`, targets macOS.

## Documents

| File | Answers |
|---|---|
| `design.md` | **What** to build and why — product and macro-architecture decisions |
| `docs/architecture.md` | **How** the code is organized — layers, boundaries, data flow |
| `docs/conventions.md` | Python and TypeScript code standards |
| `docs/design-system.md` | Visual tokens, components, UI rules |
| `docs/roadmap.md` | **Where** the project is — phase status and what comes next |
| `docs/phase-2.md` | The current phase, broken into activities with dependencies |

`design.md` is the source of truth for decisions already made. If you think one of
them is wrong, say so — don't quietly work around it.

## Invariants

These are not style preferences. Breaking any of them is an architecture bug:

1. **Nothing outside `backend/app/harnesses/` branches on the harness.**
   An `if harness == "claude-code"` in `orchestrator/`, `api/`, `ws/`, or the
   frontend means `AgentEvent` is incomplete. Fix the event, not the consumer.

2. **Every graph node executes inside its own git worktree.**
   Never run an agent directly against the target repository. Without worktrees,
   parallelism corrupts the diff.

3. **Tokens are four fields:** `input + output + cache_read + cache_write`.
   Summing only `input_tokens` makes the dashboard wrong by ~100× in a long
   session. `cost_usd` is computed at *ingest* using the price in effect at that
   moment — never in the query.

4. **NDJSON is the source of truth for a run; SQLite is a derived index.**
   It must always be possible to rebuild the SQLite row from
   `runs/<run_id>/events.ndjson`. If it isn't, the index has taken on too much
   responsibility.

5. **No blocking calls on the event loop.**
   `psutil`, `sqlite3`, `subprocess.run`, and git commands are synchronous and
   will stall the PTY stream. Use `asyncio.create_subprocess_exec`, `aiosqlite`,
   and `run_in_executor`.

6. **The planner's graph is a proposal, not an execution order.**
   Nothing runs before human approval while `auto_merge` is off.

7. **Cost is "estimated equivalent"** when the harness runs under a subscription
   (Max/Pro). Never label it "spend".

8. **Secrets never enter the agent's worktree.**
   ai-jail's `--mask` / `--deny-path` are mandatory, not optional.

## Commands

```bash
# backend
cd backend
uv sync
uv run fastapi dev app/main.py          # dev server on :8000
uv run pytest                            # harness contract tests skip without the binary
uv run pytest -m harness                 # real contract tests, requires the CLIs installed
uv run ruff format . && uv run ruff check --fix .
uv run mypy app
uv run lint-imports                      # layer contracts from docs/architecture.md §1

# frontend
cd frontend
pnpm install
pnpm dev                                 # Vite on :5173, proxying to :8000
pnpm typecheck
pnpm lint
pnpm gen:api                             # openapi-typescript + AgentEvent schema
```

Before calling anything done: `ruff check`, `mypy app`, `pytest`, and
`pnpm typecheck` must pass. Do not report "done" with a red test.

## Project state

Current phase: **Phase 2 — the graph** (see `design.md` §10 and
`docs/phase-2.md`). This is the heart of the product.

Phases 0 and 1 are complete and were accepted against a real Codex session:
sandbox, structured events, four-field usage, NDJSON replay with pinned price
versions, the persistent run service, REST, the cursor-replay WebSocket broker,
process-group kill, immutable-attempt retry, and the live session view. Claude
Code's successful acceptance run is deferred while that account is not in use;
its failure path remains covered. Channel B (PTY attach) is deliberately
deferred — B10 proved Codex's `exec --json` runtime is not attachable, and a
misleading terminal is worse than none.

Two things Phase 1 leaves standing that Phase 2 must generalize, not work
around: `SingleRunService` assumes **one active run per session**, and nothing
serializes concurrent merges into the shared integration worktree. Do not jump
ahead to dashboards or code search before the graph executes.

## Language

Code, comments, commit messages, documentation, and UI strings are in **English**.
