# Code conventions

Rules a linter can't enforce on its own. The ones it can are already configured —
don't repeat configuration here, configure it there.

---

## 1. Tooling

### Backend

| | Choice | Why |
|---|---|---|
| Python | 3.12+ | `type` statement, better `asyncio.TaskGroup` |
| Package manager | **uv** | Resolves and installs in seconds; deterministic lockfile |
| Lint + format | **ruff** (replaces black, isort, flake8) | One binary, one config |
| Types | **mypy strict** on `orchestrator/`, `harnesses/`, `storage/` | The core is what matters; `api/` can be looser |
| Architecture | **import-linter** | Layer contracts from `architecture.md` §1 |
| Tests | pytest + pytest-asyncio + golden fixtures | |
| Logging | **structlog** (JSON) | Log correlated by `run_id`, parseable |
| Models | Pydantic v2 (events, API) + SQLModel (tables) | |
| Migrations | Alembic | Even on SQLite; renaming a column without a migration is debt |

### Frontend

| | Choice | Why |
|---|---|---|
| Build | Vite + React 19 + TypeScript strict | |
| Lint + format | **Biome** | One binary instead of ESLint + Prettier |
| Server state | TanStack Query | |
| Live state | Zustand (one store per WS topic) | |
| Routing | React Router | `/dashboard`, `/sessions/:id`, `/search`, `/settings` |
| Graph | `@xyflow/react` + `elkjs` | |
| Terminal | `@xterm/xterm` + `@xterm/addon-fit` + `@xterm/addon-webgl` | |
| Tests | Vitest; Playwright on the main flows only | |

Don't add a dependency without justifying it in one sentence in the commit. Every
dependency is maintenance surface on a one-person project.

---

## 2. Python

### Identifiers and domain types

```python
type SessionId = str
type NodeId = str
type RunId = str
```

IDs are **ULIDs** with a prefix (`sess_01J…`, `node_…`, `run_…`), not UUID4. ULIDs
sort by time, which makes `ORDER BY id` work and keeps logs readable.

If a parameter is a path, its type is `pathlib.Path` — never `str`. Conversion
happens at the boundary (config/CLI parsing), not in the middle of the logic.

### Time

Timestamps are **int, milliseconds, UTC**. One helper:

```python
def now_ms() -> int:
    return int(time.time() * 1000)
```

Never `datetime.now()` without a timezone. Never persist local time. Timezone
formatting is the frontend's problem.

### Async

- Subprocesses: `asyncio.create_subprocess_exec` — **never** `subprocess.run`,
  `os.system`, or `shell=True`.
- argv is always a `list[str]`. No concatenated strings with spaces.
- Unavoidable synchronous calls (`psutil`, git, `sqlite3`) go through
  `asyncio.to_thread` / `run_in_executor`.
- Task groups: `async with asyncio.TaskGroup()`, not a loose `gather` — TaskGroup
  cancels siblings when one fails, which is the behavior you want in a scheduler.
- Every long-running task is cancellable. `CancelledError` is re-raised, never
  swallowed:

```python
except asyncio.CancelledError:
    await cleanup()
    raise            # required
```

### Events

Discriminated union, one `type` literal per variant:

```python
class Usage(BaseModel):
    type: Literal["usage"] = "usage"
    run_id: RunId
    ts: int
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

AgentEvent = Annotated[
    RunStarted | AssistantText | ThinkingDelta | ToolCall | ToolResult
    | Usage | Permission | RunFinished | RawChunk,
    Field(discriminator="type"),
]
```

Consume with an exhaustive `match`. No chains of `if event.type == ...` — a `match`
with a raising `case _:` forces you to handle new variants.

A new field on an existing event is **optional with a default**. Old NDJSON must
keep loading (replay is an invariant, not a feature).

### Errors

```python
class AgentHubError(Exception): ...
class HarnessError(AgentHubError): ...
class WorktreeConflict(AgentHubError): ...
```

Always with context in the message (`run_id`, path, argv). Use `raise ... from err`
so the chain survives. Never a bare `except:`, never `except Exception: pass`.

### Logging

```python
log = structlog.get_logger()
log = log.bind(run_id=run_id, node_id=node_id, harness=harness)
log.info("harness.started", pid=proc.pid, model=model)
```

Event names are `domain.action`, lowercase snake_case, and stable — they are search
keys. Context goes in fields, not interpolated into the string. Never log masked
file contents, environment variables, tokens, or the full prompt (hash and length
only).

### Configuration

One `Settings(BaseSettings)` in `app/config.py`, read once. No `os.getenv()`
scattered around. Defaults must work without a `.env` — the project has to run on a
clean machine with `uv run fastapi dev`.

### Docstrings and comments

Docstring when the *why* isn't obvious from the signature. Comments explain
decisions, not mechanics:

```python
# bad
i += 1  # increment i

# good
# Batched fsync: per-line drops event throughput from ~5k/s to ~200/s
```

No decorative section banners (`# ===== SETUP =====`). No docstring that restates
the function name.

---

## 3. TypeScript / React

### Hard rules

- `strict: true`, `noUncheckedIndexedAccess: true`. **Zero `any`** — use `unknown`
  and narrow.
- No `export default` (except where Vite requires it). Named exports make rename
  and search work.
- No barrel files (`index.ts` re-exporting a whole folder) — they break
  tree-shaking and create import cycles.
- Backend types come only from `src/api/schema.d.ts` and `src/api/events.d.ts`
  (generated). Hand-writing a mirror of a Python model is forbidden
  (`architecture.md` §7).

### Components

```
components/<domain>/ComponentName.tsx    # one component per file, same name
```

- Declared functions, not arrow functions assigned to a const.
- Props typed inline as `type Props`, no `React.FC`.
- Components that fetch data live in `routes/`. Components in `components/` receive
  props.
- Past ~150 lines or 3 `useEffect`s, it's doing too much — split it.

### `useEffect`

Only to synchronize with an external system (WebSocket, xterm, ELK layout,
`ResizeObserver`). **Not** to derive state — that's a render-time computation or a
`useMemo`. Every `useEffect` that subscribes returns a cleanup.

### Live state

One WebSocket handler per topic, writing into a store. Components read with a
narrow selector:

```ts
// good: re-renders only when that node's status changes
const status = useGraphStore(s => s.nodes[nodeId]?.status)

// bad: re-renders on every PTY chunk from any node
const store = useGraphStore()
```

In an app receiving hundreds of events per second, a broad selector is what freezes
the tab.

### Performance where it matters

- Graph: `React.memo` on the custom node; run the ELK layout only when the topology
  changes, not on every event.
- Terminal: xterm manages its own DOM. Never re-render the component hosting it —
  mount it once in a ref and write via `term.write()`.
- Event feed: virtualize (`@tanstack/react-virtual`) past ~200 rows.

---

## 4. Naming

| Thing | Convention | Example |
|---|---|---|
| Python module | snake_case, singular | `worktree.py` |
| Class | PascalCase | `ClaudeCodeAdapter` |
| Log event | `domain.action` | `worktree.merge_conflict` |
| SQL table | snake_case singular | `usage_event`, `node` |
| SQL index | `ix_<table>_<columns>` | `ix_usage_session_ts` |
| REST route | plural, kebab-case | `/api/sessions/{id}/nodes` |
| WS topic | `<resource>:<id>` | `run:run_01J…` |
| React component | PascalCase | `NodeDrawer.tsx` |
| Hook | `use<Thing>` | `useRunStream.ts` |
| Generated git branch | `agenthub/<sess>/<node>` | |

Fixed vocabulary across the codebase, no synonyms: **session, graph, node, run,
event, harness, model, worktree, integration branch, final branch**. The
integration branch is AgentHub's temporary merge target; the final branch is
the operator-named durable result. If a section calls a `node` a "task" or a
"step", rename it. Synonyms in a small domain are bugs waiting to happen.

---

## 5. Commits

Conventional Commits, scope = module:

```
feat(harnesses): codex adapter with --json parsing
fix(scheduler): don't mark a node done when the merge fails
test(worktree): merge conflict becomes blocked state
chore(deps): sqlite-vec 0.2
```

A commit is a reviewable unit: one change, tests included, green. Don't mix a
refactor with a feature — that makes the diff unreadable exactly when you need it
most.

---

## 6. Security (not optional in this project)

You are executing shell-capable agents inside real repositories.

- **Always** build the ai-jail argv with `--mask .env --mask '*.pem'` and
  `--deny-path ~/.aws --deny-path ~/.ssh`. Default deny; the policy is explicit,
  never empty.
- Never pass a secret through argv (it shows up in `ps`). Use the child process
  environment or a `0600` file.
- A run's `meta.json` records a **sanitized** environment — allowlist of keys, not
  a denylist.
- Bind to `127.0.0.1`. No auth in the MVP means it must not listen on `0.0.0.0` —
  not by accident, not in dev.
- Paths from the frontend are never used directly: resolve and verify they are
  inside `~/.agenthub/workspaces/<session_id>/`.
- Agent output is untrusted content. Render markdown through a sanitizer and never
  use `dangerouslySetInnerHTML` without one.
