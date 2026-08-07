# Phase 1 acceptance record

Validated on 2026-08-07 on macOS with:

- Codex CLI 0.146.0, authenticated through ChatGPT;
- ai-jail 1.16.0;
- uv 0.12.1 and Python 3.13.5;
- Node 26.6.0 and pnpm 11.20.0.

The target was a disposable Git repository at commit
`c2b51fb8e98be345ead6dac9d7b165535f58deda`. AgentHub used a fresh runtime root,
so the acceptance run did not read or mutate an existing AgentHub database or
workspace.

## End-to-end path

The session was created through `POST /api/sessions` with Codex
`gpt-5.6-terra`, `auto_merge=false`, and one fixed node. One browser-equivalent
WebSocket subscribed to the session topic before the first run started.

The first attempt entered a real command tool call. The client disconnected
after sequence 5, reconnected with that exact stream/cursor, then requested
`POST /kill`. The reconnect received sequence 6 on the same stream, proving
there was no event gap or duplicate at the transport boundary. The durable
terminal event classified the run as `interrupted`.

`POST /retry` created attempt 2 rather than mutating attempt 1. The same node
worktree was reused, Codex completed successfully, and the node stopped at
`awaiting_review`; no merge occurred without human approval. Its checkpoint
contains both `.agenthub-b11-marker` and `acceptance.txt`, whose content is
exactly:

```text
phase-1 acceptance passed
```

## Durable results

| Attempt | Run | Status | Events | Input | Output | Cache read | Cache write | Estimated equivalent | Trusted |
|---:|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | `run_01KZD7M1N6QJ3HA0JYHWG65RBE` | interrupted | 6 | 0 | 0 | 0 | 0 | unknown | yes |
| 2 | `run_01KZD7M9H51BZTDEWHQVP9TFZT` | success | 12 | 9,060 | 485 | 62,208 | 0 | $0.045477 | yes |

For each run, the REST event count equalled the SQLite `run.event_count`. The
four token fields summed from persisted `usage` events equalled the
`/summary` response consumed by the UI. SQLite held one append-only usage row
for attempt 2, priced at ingest with table version 1; attempt 1 correctly had no
usage row because it was killed before Codex reported turn usage.

Both projections were then discarded and rebuilt independently:

```text
agenthub replay run_01KZD7M1N6QJ3HA0JYHWG65RBE
  events=6 usage_rows=0 status=interrupted
  in=0 out=0 cache_read=0 cache_write=0

agenthub replay run_01KZD7M9H51BZTDEWHQVP9TFZT
  events=12 usage_rows=1 status=success price_table_version=1
  in=9060 out=485 cache_read=62208 cache_write=0
  estimated_equivalent_cost_usd=0.045477
```

The post-replay REST summaries and direct SQLite queries returned the same
values. The authored session and node remained present and the node remained
`awaiting_review`, confirming replay touched only derived run/usage rows.

The production `TokenSummary` component has a regression test using the real
successful-run payload. It renders `72K tokens`, the four categories (`62K`,
`0`, `9.1K`, and `485`), and `$0.0455` estimated equivalent cost. This closes
the last database → REST → generated TypeScript → UI formatting boundary.

## Result

Phase 1 is accepted. The persistent single-node path, sandboxed real harness,
HTTP lifecycle, bounded/reconnectable WebSocket, kill, immutable retry, NDJSON
replay, four-field token accounting, equivalent cost, checkpoint, and manual
merge gate all stood up together.

Channel B remains the explicit B10 deferment: Codex app-server is interactive,
but it is not a live sidecar for the stable `codex exec --json` adapter. No
terminal is advertised for this accepted Phase 1 path.
