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
| `docs/phase-<n>.md` | Each phase, broken into activities with dependencies and results |
| `docs/acceptance-phase-<n>.md` | What was actually run to accept a phase, and what it does *not* claim |

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
   (Max/Pro). Never label it "spend". The planner is the one component that can
   be either: real spend on the `api` backend, estimated equivalent on the
   `harness` one. Read `planner_backend` — never assume.

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

**The MVP is complete.** Phases 0 through 4 are accepted, each against real
runs rather than fixtures — see `docs/roadmap.md` for the summary and
`docs/acceptance-phase-{1,2,3,4}.md` for the evidence.

What is deliberately *not* done, so nobody "fixes" it by accident:

- **Channel B (PTY attach) is deferred.** B10 proved Codex's `exec --json`
  runtime is not attachable; a terminal that shows something other than the
  live run is worse than no terminal.
- **Claude Code's successful acceptance run is deferred** while that account is
  not in use. Its failure path is covered.
- **No live-provider turn is claimed for Phase 4's agentic loop.** There was no
  API key on the acceptance machine, and `acceptance-phase-4.md` says so rather
  than implying coverage it does not have.
- **Planner usage is not recorded in `usage_event`.** It structurally cannot be:
  `run_id`, `session_id` and `harness` are all `NOT NULL`. Phase 2's C8 result
  lists the four changes that would make it recordable. This is unchanged by
  the planner's harness backend — a plan is still not a node, and giving it a
  `run_id` to satisfy the schema would put a row in the dashboard for something
  that has no worktree and no diff.

Two environment traps that have already cost time here:

- **`rg` may be a shell function.** `which rg` and `rg --version` succeed while
  `subprocess` cannot find it. Verify with `ls -l "$(command -v rg)"`.
- **SQLAlchemy skips `greenlet` on Apple Silicon** because its marker lists
  `aarch64` and not `arm64`; the `sqlalchemy[asyncio]` extra is what pulls it in.

## Language

Code, comments, commit messages, documentation, and UI strings are in **English**.
