# Phase 2 acceptance record

Validated on 2026-08-08 on macOS with:

- Codex CLI 0.146.0, authenticated through ChatGPT;
- ai-jail 1.16.0;
- uv 0.12.1 and Python 3.13.5;
- Node 26.6.0 and pnpm 11.20.0.

The target was a disposable Git repository at commit
`24a3b0047c57b2a3ff6682ad0fcbd456f2d1e7d8`. AgentHub used a fresh runtime root,
database, workspaces, and run log. No existing repository or AgentHub state was
read or mutated.

## End-to-end graph

The acceptance client submitted one objective to `POST /api/graphs/plan`: run
two independent Codex activities concurrently and make their `shared.txt`
edits conflict at the approval gate. The paid Anthropic boundary used a
deterministic recorded structured response, as required by C8's acceptance
contract; graph creation, validation, persistence, editing, approval,
scheduling, worktrees, harness execution, review, and merge were production
code. No Anthropic credential was used or billed during this run.

The proposal had two independent roots, `alpha` and `beta`, both using Codex
`gpt-5.6-terra`. Each was assigned its own worktree and asked to replace the
same line in `shared.txt` with a different value while creating a branch-unique
file. Before approval, `POST /api/graphs/{session_id}/runs` returned 409. The
human then edited `alpha` through the node replacement route, reloaded the
persisted value, approved the graph, and scheduled it.

Both real processes started within 7 ms of each other. Their execution windows
overlapped for 16,730 ms, proving that the scheduler used its two concurrency
slots rather than serializing nodes. Both runs completed successfully, were
trusted, had no permission denials, committed two files in their isolated
worktrees, and stopped at `awaiting_review`; nothing merged automatically.

The reviewer marked both acceptance criteria on `alpha` as passing and approved
it. Its commit merged into the session integration branch and the node became
`done`. The same review and approval on `beta` then produced the deliberate
conflict in `shared.txt`; the merge was aborted cleanly and the node became
`blocked`. This is the expected Phase 2 terminal state because a resolver agent
is explicitly out of scope.

## Durable results

Session: `sess_01KZFTE0YV6M45PHF9Y940PJBB`

| Node | Run | Status | Events | Input | Output | Cache read | Cache write | Estimated equivalent | Trusted |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| alpha | `run_01KZFTE126TS0270CJ5EZRZMJV` | success | 15 | 9,467 | 709 | 62,208 | 0 | $0.0498545 | yes |
| beta | `run_01KZFTE12WC6KD2P1AKT8TJARY` | success | 13 | 7,978 | 573 | 49,152 | 0 | $0.0408280 | yes |

Every run's REST event count equalled the SQLite `run.event_count`. Direct
SQLite aggregation found one append-only `usage_event` per run, both priced at
ingest with price-table version 1, and returned the same four token fields and
costs as the node-addressed REST summaries consumed by the drawer.

## NDJSON replay and UI projection

Both run projections were discarded and rebuilt independently from their
canonical logs:

```text
agenthub replay run_01KZFTE126TS0270CJ5EZRZMJV
  events=15 usage_rows=1 status=success price_table_version=1
  in=9467 out=709 cache_read=62208 cache_write=0
  estimated_equivalent_cost_usd=0.0498545

agenthub replay run_01KZFTE12WC6KD2P1AKT8TJARY
  events=13 usage_rows=1 status=success price_table_version=1
  in=7978 out=573 cache_read=49152 cache_write=0
  estimated_equivalent_cost_usd=0.0408280
```

After replay, a fresh application process returned the same run ids, statuses,
event counts, four token fields, totals, costs, and trust flags through REST.
The authored graph and reviews survived untouched with `alpha=done` and
`beta=blocked`, confirming replay only rebuilt derived run and usage rows.

`TokenSummary` now has regression fixtures containing both real REST payloads.
They render totals of `72K tokens` and `58K tokens`, every individual token
category, and `$0.0499`/`$0.0408` estimated-equivalent costs. This closes the
SQLite → REST → generated TypeScript → per-node drawer presentation boundary.

## Result

Phase 2 is accepted. An objective became an editable and gated graph; the
approved graph ran real agents concurrently in isolated worktrees; human review
serialized integration merges; a real conflict became durable blocked state;
and both NDJSON logs rebuilt projections that agreed with SQLite, REST, and UI.
