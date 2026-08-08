# Phase 3 acceptance record

Validated on 2026-08-08 on macOS with:

- Codex CLI 0.146.0;
- ai-jail 1.16.0;
- uv 0.12.1 and Python 3.13.5;
- Node 26.6.0 and pnpm 11.20.0.

The source was the preserved, accepted Phase 2 runtime documented in
[`acceptance-phase-2.md`](acceptance-phase-2.md). Validation copied that entire
runtime to a fresh directory under `/private/tmp` before migration. The original
database, run logs, and worktrees were read but never mutated.

## Migration and durable dashboard

The copy upgraded from `e9f4b9cfa8c1` to `b7c3d9e51f24` without rebuilding an
existing table. The migration added the minute aggregate and node transition
tables, then backfilled one honest current-state event per meaningful existing
node from `node.status` and `node.updated_ms`:

| Node | Feed state |
|---|---|
| alpha | `done` |
| beta | `blocked` |

`GET /api/dashboard?period=30d` returned the accepted active session
`sess_01KZFTE0YV6M45PHF9Y940PJBB`, one active session, and the same ingest-time
usage as direct SQLite aggregation:

| Input | Output | Cache read | Cache write | Total | Estimated equivalent cost |
|---:|---:|---:|---:|---:|---:|
| 17,445 | 1,282 | 111,360 | 0 | 130,087 | $0.0906825 |

The generated response also returned `beta=blocked` followed by `alpha=done` in
newest-first order. The dashboard route regression test consumes that generated
shape, renders the canonical state icon and label, and verifies the exact
`/sessions/:id?node=:node_id` link. A graph workspace test proves that URL opens
the requested node drawer rather than merely landing on the session.

## Live metrics and restart

A browser-equivalent client subscribed to `metrics` over the production
multiplexed WebSocket. It received a real psutil snapshot containing non-zero
host memory and worktree-disk totals; no REST polling supplied the live frame.

Closing the first application process flushed its partial UTC-minute bucket as
one `system_metric_minute` row with `sample_count=1`. A fresh process opened the
same database, sampled again within that minute, and closed. The table still had
exactly one row and its count became 2. Thus restart preserved history and
merged the partial bucket; it did not persist two one-second rows. The live
browser ring remained separately bounded at 300 samples.

## NDJSON replay isolation

Both canonical Phase 2 logs were replayed after the D5 migration:

```text
run_01KZFTE126TS0270CJ5EZRZMJV
  events=15 usage_rows=1 status=success
  in=9467 out=709 cache_read=62208 cache_write=0 cost=$0.0499

run_01KZFTE12WC6KD2P1AKT8TJARY
  events=13 usage_rows=1 status=success
  in=7978 out=573 cache_read=49152 cache_write=0 cost=$0.0408
```

After replay, SQLite still held the one minute row with `sample_count=2` and the
same two transition rows. The four usage fields still summed to
`17,445 + 1,282 + 111,360 + 0`, with stored cost `$0.0906825`. This proves run
replay continued to rebuild only the run and usage projections; host telemetry
and orchestration transitions did not acquire a false NDJSON dependency.

## Result

Phase 3 is accepted. Durable token/cost projection, active graph progress, live
system/process telemetry, bounded raw history, restart-safe minute aggregates,
and the deep-linked meaningful event feed agree across SQLite, REST, generated
TypeScript, WebSocket, and UI tests without persisting one-second telemetry.
