# `src/api/`

## Generated, committed, never hand-edited

Two files in this directory are produced by `pnpm gen:api` and committed to the
repository (see the note in the repo `.gitignore` and `docs/architecture.md` §7):

| File | Source | Generator |
|---|---|---|
| `schema.d.ts` | FastAPI `/openapi.json` | `openapi-typescript` |
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

## Why the files are not here yet

`pnpm gen:api` needs a running backend that serves `/openapi.json` (Phase 1, B5)
and the schema-export script (B8's backend half). The tooling and the script are
already wired — `frontend/scripts/gen-api.mjs` — so generation is a single
command once those exist. A placeholder that drifts would be worse than an
absent file, so nothing is checked in until the generator can run.

## Not generated

`query-client.ts` is hand-written configuration for TanStack Query. It contains
no backend types.
