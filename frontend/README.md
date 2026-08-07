# AgentHub frontend

Vite + React 19 + TypeScript (strict) + Tailwind v4 + shadcn/ui on Base UI.
Dark-only, high density — `docs/design-system.md` is the specification.

```bash
pnpm install
pnpm dev         # 127.0.0.1:5173, proxies /api and /ws to 127.0.0.1:8000
pnpm typecheck
pnpm lint        # Biome: lint + format, one binary
pnpm format      # Biome with --write
pnpm build
pnpm gen:api     # regenerate src/api/*.d.ts from exported backend schemas
```

## Layout

```
src/
  api/            query-client.ts + the generated, committed schema.d.ts / events.d.ts
  components/
    layout/       the application frame (top bar, tabs, empty state)
    ui/           shadcn primitives; density is tuned here, once, not per call site
  lib/            cn(), the node-state vocabulary of design-system §5
  routes/         one file per route + router.tsx; routes fetch, components receive props
  stores/         Zustand — live state only
  styles/         tokens.css (design-system §2) and globals.css
  ws/             the single WebSocket connection: protocol, client, React provider
scripts/          gen-api.mjs
```

## Three state sources, never mixed

| Source | Tool |
|---|---|
| Server (sessions, graphs, history) | TanStack Query |
| Live (events, metrics, PTY) | one WebSocket connection → Zustand |
| UI (open drawer, zoom, filter) | `useState` / local store |

`docs/architecture.md` §6. A structural event invalidates a query; a stream
event updates a store.

## Rules that fail review

- No raw colour values. Every colour comes from `src/styles/tokens.css`.
- No pixel values outside the 4/6/8/12/16/24/32 scale, except the control
  dimensions fixed in design-system §4.
- No hand-written TypeScript mirroring a Python model — only the generated
  files in `src/api/`.
- No `any`, no `export default` (except where a tool demands it), no barrel
  files.
