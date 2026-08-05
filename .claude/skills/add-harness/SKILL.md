---
name: add-harness
description: Add a new harness (agent CLI) to AgentHub following the BaseHarnessAdapter contract — real fixture capture, parser, golden tests, contract tests, registration, and pricing. Use when integrating Codex, OpenCode, Crush, Gemini CLI, or any other agent CLI.
---

# Adding a harness

The order is mandatory. Jumping straight to the adapter produces a speculative
parser that fails on the first real run.

## 1. Verify the binary

```bash
which <cli> && <cli> --version
<cli> --help
```

If it isn't installed, **stop and say so**. Do not write a parser from
documentation — the flags change between releases.

## 2. Capture real fixtures

Create a throwaway git repo, run the CLI in structured mode, and record the raw
output:

```bash
mkdir -p backend/tests/fixtures/<harness>
cd /tmp && rm -rf fx && mkdir fx && cd fx && git init -q && echo x > a.txt && git add -A && git commit -qm init

<cli> <structured-output-flags> "create a file b.txt containing 'hello'" \
  > $REPO/backend/tests/fixtures/<harness>/simple_edit.ndjson
```

Capture at least four cases:

| Case | Why |
|---|---|
| `simple_edit` | Happy path with a tool call and a write |
| `tool_error` | Tool result reporting failure |
| `multi_turn` | Message injection mid-session |
| `interrupted` | Interrupt/kill partway through |

Sanitize the fixtures: strip absolute home paths, keys, and any tokens.

## 3. Map the events

Before coding, write the translation table as a comment at the top of the adapter:

```
CLI line                           →  AgentEvent
{"type":"system","subtype":"init"} →  RunStarted
{"type":"assistant", ...}          →  AssistantText / ToolCall / Usage
...
```

Decisions that need an explicit answer from the fixture, not from intuition:

- **Is usage cumulative or incremental per message?** Getting this wrong doubles
  the total.
- Are all four token fields present? What is each one called in this CLI?
  (`cache_creation_input_tokens` → `cache_write_tokens`,
   `cache_read_input_tokens` → `cache_read_tokens`)
- Does the CLI emit a final event with status and exit code?
- What is the format for injecting a message on stdin?
- Does it request permissions? That becomes `Permission`.

Information only this CLI has: **generalize it into the event or drop it**. Never
expose the harness name as a downstream conditional.

## 4. Implement the adapter

`backend/app/harnesses/<harness>.py`, implementing `BaseHarnessAdapter`: `start`,
`send`, `interrupt`, `kill`, `events`.

- argv built as a `list[str]` and passed through `sandbox/aijail.py`.
- `asyncio.create_subprocess_exec` with `start_new_session=True`.
- `kill` uses `os.killpg` to take down the tree.
- `CancelledError` → kill the process and re-raise.
- Channel B (PTY) optional, producing `RawChunk`, with a bounded queue that drops
  from the middle.

## 5. Golden test

```python
@pytest.mark.parametrize("case", ["simple_edit", "tool_error", "multi_turn", "interrupted"])
def test_parse_golden(case: str):
    raw = (FIXTURES / f"{case}.ndjson").read_text()
    events = list(parse_stream(raw))
    assert_golden(events, FIXTURES / f"{case}.expected.json")
```

This is the test that warns you when the next CLI release changes the format.

Add a token-specific test that sums all four fields and compares against the total
the CLI itself reports in its final event.

## 6. Contract test

```python
@pytest.mark.harness
async def test_real_cli_roundtrip(tmp_git_repo): ...
```

Marked `harness`, skipped automatically when the binary is absent. Runs with
`uv run pytest -m harness`.

## 7. Registration and catalog

- Register it in the harness registry (`harnesses/__init__.py`).
- Add the supported models to the catalog — that's what populates the per-node
  model selector in Tab 2.
- Add prices to `pricing.yaml`. Without an entry there, `cost_usd` stays null.
- Document the flags used and the version tested at the top of the adapter.

## 8. Final verification

```bash
cd backend
uv run pytest tests/harnesses/test_<harness>.py
uv run mypy app/harnesses
uv run lint-imports
grep -rn '"<harness>"' app/ --include='*.py' | grep -v 'app/harnesses/'   # must be empty
```

That last command checks the invariant: the harness name must not appear outside
its own layer.
