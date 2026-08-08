# Phase 4 acceptance

Accepted on 2026-08-08 against detached AgentHub commit `5aba943`.

## Scope and method

`backend/scripts/accept_phase4.py` created a migrated temporary database whose
persisted session pointed at a real detached Git integration worktree. It ran
the production symbol and semantic services over that worktree, repeated the
semantic synchronization, queried two concepts, resolved a symbol through the
index, and re-read its exact line range through `CodeSearchService`.

No `ANTHROPIC_API_KEY` was available on this machine, so this record does not
claim a paid live-provider turn. The model loop itself is covered by deterministic
contract tests: a multi-hop answer can exit only through `submit_answer`, unread
citations are rejected, each independent ceiling returns a partial result, and
`semantic_search` is refused before a primary navigation attempt. This acceptance
focuses on the repository-dependent behavior those tests cannot prove.

## Results

| Check | Result |
|---|---|
| Supported files indexed by Tree-sitter | 168 |
| Supported files indexed semantically | 168 |
| Files re-embedded on unchanged second pass | 0 |
| `evidence citation read file validated answer` | `backend/app/search/agent.py`, rank 2 |
| `incremental symbol tree sitter tags parser` | `backend/app/search/symbols.py`, rank 1 |

The symbol lookup resolved `SearchLimitReason` at
`backend/app/search/agent.py:37`. Reading exactly line 37 produced SHA-256
`abb38ce1aabb61e5b3e3c0bdd66046c96a23f2e8d149ce41c7394031847f2af9`.
The path, one-based range and line-content hash therefore agreed between the
index and the bounded file tool at the searched revision.

## Fault found during acceptance

The first repository-wide pass exposed a native Tree-sitter `QueryCursor`
segmentation fault after several heterogeneous files. Small fixtures had hidden
it. The accepted implementation performs each changed-file parse in a recycled
one-task worker process; a native parser failure can no longer terminate the
orchestrator. Hash-identical files still bypass parsing entirely. The final
acceptance completed over all 168 files without a native crash.

## Reproduce

Create a detached worktree whose directory is named `integration`, then run:

```bash
cd backend
uv run python scripts/accept_phase4.py /path/to/workspace/integration 5aba943
```

The runner fails if either expected file leaves the semantic top five, if the
symbol index cannot resolve a definition, or if the exact citation cannot be
read.
