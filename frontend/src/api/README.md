# `src/api/`

## Generated, committed, never hand-edited

Two files in this directory are produced by `pnpm gen:api` and committed to the
repository (see the note in the repo `.gitignore` and `docs/architecture.md` §7):

| File | Source | Generator |
|---|---|---|
| `schema.d.ts` | exported FastAPI `/openapi.json` | `openapi-typescript` |
| `events.d.ts` | `AgentEvent.model_json_schema()` | `json-schema-to-typescript` |

```
FastAPI    ──► /openapi.json ──► openapi-typescript      ──► src/api/schema.d.ts
AgentEvent ──► model_json_schema() ──► json-schema-to-typescript ──► src/api/events.d.ts
```

`AgentEvent` travels over the WebSocket, so it never appears in the OpenAPI
document. It is exported separately by `backend/scripts/export_schemas.py`.

**Hand-writing a TypeScript type that mirrors a Python model is forbidden.** The
mirror always drifts, and the drift shows up as an `undefined` field in the
middle of a stream. If you need a backend type that is not here, the fix is to
make the backend emit it, then regenerate.

## Regeneration

Export the canonical backend documents, then generate the TypeScript mirrors:

```bash
cd backend && uv run python scripts/export_schemas.py
cd ../frontend && pnpm gen:api
```

CI runs both commands with `--check`; neither needs a running server.

## Not generated

`query-client.ts` is hand-written configuration for TanStack Query. It contains
no backend types.
