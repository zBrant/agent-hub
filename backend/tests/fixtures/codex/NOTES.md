# Codex fixture notes

Captured from `codex-cli 0.146.0` on macOS with:

```bash
codex exec --json --ignore-user-config --ignore-rules \
  --sandbox workspace-write -C /tmp/repo "<prompt>"
```

The captures were made in a disposable git repository. Absolute temporary paths
were replaced with `/tmp/repo`; no credentials or environment values appear in
the fixtures.

## Observed protocol

- Stdout is clean JSONL. The `Reading additional input from stdin...` notice and
  runtime diagnostics go to stderr.
- Every invocation starts with `thread.started`, including `exec resume`; the
  resumed invocation repeats the same `thread_id`.
- A successful turn ends with `turn.completed`. The CLI process supplies the
  exit code separately; no JSON event contains it.
- `item.started` and `item.completed` share an item id. Command completion has
  `aggregated_output`, `exit_code`, and a terminal status. File changes carry a
  list of paths and `add`/`update`/`delete` kinds but no file contents.
- `input_tokens` includes `cached_input_tokens`; cache reads are a breakdown,
  not an additional quantity. `reasoning_output_tokens` is likewise a breakdown
  of `output_tokens`. AgentHub subtracts cached input before mapping the four
  mutually exclusive dashboard fields.
- Usage is per turn. A resumed turn naturally has a larger input because it
  sends conversation history again; it is not a cumulative session counter.
- `cache_write_input_tokens` is present in these 0.146.0 captures. Older CLI
  releases may omit it, so the parser defaults it to zero.
- Interrupting the foreground process with Ctrl-C exits 1 and produces no
  terminal JSON event. The partial `interrupted.ndjson` ends at an in-progress
  command; the adapter must remember that it requested the interrupt and
  synthesize `RunFinished(status="interrupted")` from process state.
