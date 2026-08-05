---
name: agent-event
description: Change the AgentEvent schema safely — new variant, new field, TypeScript type regeneration, and NDJSON replay compatibility. Use whenever touching harnesses/events.py.
---

# Changing the `AgentEvent` schema

`AgentEvent` is the system's central boundary (`docs/architecture.md` §2) and is
serialized in three places: `events.ndjson`, WebSocket frames, and replay. Changing
this type carelessly breaks the history of sessions that already ran.

## Compatibility rule

**Every NDJSON file already on disk must keep loading.** That means:

| Change | Allowed? |
|---|---|
| Add a new variant | Yes |
| Add an **optional field with a default** | Yes |
| Add a required field | **No** — breaks replay |
| Rename a field | **No** — add a new field and deprecate |
| Remove a field | Only after a cycle of being optional and unread |
| Change a field's type | **No** |

If you genuinely need an incompatible change, write an NDJSON migrator in
`backend/scripts/migrate_events.py` and version the file with a `schema_version`
field on the first line. That is expensive — avoid it.

## Steps

### 1. Confirm it's an event, not state

Triage question: **does any consumer need this in real time, or only at the end?**
If only at the end, it belongs on `Run` or `Node` in SQLite, not in a new event.
Events are expensive — each one goes to disk, the WebSocket, and replay.

### 2. Edit `backend/app/harnesses/events.py`

```python
class NewEvent(BaseModel):
    type: Literal["new_event"] = "new_event"
    run_id: RunId
    ts: int
    # new fields always with a default
    detail: str | None = None
```

Add it to the union. The `type` discriminator is required and its literal must be
unique.

### 3. Find every exhaustive `match`

```bash
grep -rn "case RunStarted\|case Usage\|match event" backend/app/
```

Every `match` over `AgentEvent` must handle the new variant or have a raising
`case _:`. If any has `case _: pass`, the new event disappears silently — fix that
before continuing.

### 4. Regenerate the frontend types

```bash
cd backend && uv run python scripts/export_schemas.py
cd ../frontend && pnpm gen:api
git diff --stat frontend/src/api/
```

`frontend/src/api/events.d.ts` is **generated**. Never edit it by hand, never write
a manual mirror (`docs/architecture.md` §7).

### 5. Handle it in the frontend

The event store's reducer needs the new variant too. TypeScript will point it out
if the `switch` is exhaustive with a `never` default:

```ts
default: {
  const _exhaustive: never = event
  throw new Error(`unhandled event: ${JSON.stringify(_exhaustive)}`)
}
```

If that guard isn't there, add it — it's what makes the compiler do the work next
time.

### 6. Replay compatibility test

```bash
cd backend && uv run pytest tests/storage/test_replay.py
```

That test loads old NDJSON fixtures and verifies they still parse. If it doesn't
exist yet, **create it now** — it is the only guardrail against breaking history.

Add a fixture containing the new variant as well, to cover the write path.

### 7. Final verification

```bash
cd backend && uv run mypy app && uv run pytest
cd ../frontend && pnpm typecheck
git status --short frontend/src/api/    # generated files must be committed
```
