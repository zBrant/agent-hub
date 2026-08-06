# Claude Code stream-json — captured format notes

**CLI version:** `2.1.222` (`claude --version` → `2.1.222 (Claude Code)`)
**Captured:** 2026-08-05, macOS (Darwin 24.6.0), Phase 0 / activity A3.
**Auth:** subscription (`"apiKeySource":"none"` in every `system/init`) — relevant
to invariant 7: `total_cost_usd` is an *estimated equivalent*, not a charge.

Every byte in these fixtures came from running the real binary. Nothing here was
written by hand or derived from documentation.

## 1. How each fixture was produced

Throwaway repo, never the AgentHub tree:

```bash
mkdir -p /tmp/agenthub-fx && cd /tmp/agenthub-fx && rm -rf repo && mkdir repo && cd repo
git init -q && printf 'hello\n' > a.txt && git add -A && git commit -qm init
```

`ai-jail` was deliberately **not** used — sandbox integration is A6, and mixing
the two makes it impossible to tell whose output you are reading.

| Fixture | Lines | Process exit | argv (after `claude`) | Prompt / driver |
|---|---|---|---|---|
| `simple_edit.ndjson` | 14 | 0 | `-p --output-format stream-json --verbose --model claude-haiku-4-5 --permission-mode bypassPermissions` | "Read a.txt and then create b.txt containing the single word: world" |
| `tool_error.ndjson` | 15 | 0 | same as above | "(1) use the Read tool on ./does-not-exist.txt (2) run the bash command 'exit 3' …" |
| `permission_denied.ndjson` | 14 | 0 | `-p --output-format stream-json --verbose --model claude-haiku-4-5` (**no** `--permission-mode`) | same prompt as `simple_edit` |
| `multi_turn.ndjson` | 23 | 0 | `-p --output-format stream-json --input-format stream-json --verbose --model claude-haiku-4-5 --permission-mode bypassPermissions` | two messages injected on stdin (see §7) |
| `interrupted.ndjson` | 20 | 1 | same as `multi_turn` | long bash chain, then `control_request`/`interrupt` on stdin after 14 s |
| `budget_error.ndjson` | 12 | 1 | `… --permission-mode bypassPermissions --max-budget-usd 0.001` | multi-step prompt, cut off by the budget |
| `partial_messages.ndjson` | 43 | 0 | `… --include-partial-messages …` | "Read a.txt and say what it contains in one short sentence." |

Two flag findings worth recording up front:

- **`--permission-mode` is required for non-interactive tool use to succeed.**
  Without it, `permissionMode` is `"default"` and every write is refused. That
  refusal is `permission_denied.ndjson`, kept on purpose — see §5.
- `--dangerously-skip-permissions` was not needed; `--permission-mode
  bypassPermissions` was accepted directly and is the narrower flag.

### Sanitization applied

Pure substring replacement on the raw bytes, no JSON re-serialization, so the
shape stays byte-faithful:

| From | To |
|---|---|
| `$HOME/.claude/projects/-private-tmp-agenthub-fx-repo/memory/` | `/tmp/home/.claude/projects/-tmp-repo/memory/` |
| `/private/tmp/agenthub-fx/repo`, `/tmp/agenthub-fx/repo` | `/tmp/repo` |

That was the only home path present (`memory_paths.auto` in `system/init`). No
API keys, bearer tokens or hostnames appear anywhere in the captures. `session_id`
and `uuid` values are kept — they are per-run identifiers that the parser needs to
see, not secrets.

**Not sanitized, but not part of the format contract:** `system/init` carries
`tools`, `skills`, `slash_commands`, `agents`, `mcp_servers` from the capturing
machine's user config. The parser must not assume those specific values.

## 2. The exact set of top-level line shapes

Every line is a single JSON object with a `type`. Counted across all seven
fixtures:

| count | `type` | `subtype` |
|---|---|---|
| 34 | `assistant` | — |
| 15 | `user` | — |
| 40 | `system` | `thinking_tokens` |
| 8 | `system` | `init` |
| 2 | `system` | `status` |
| 7 | `rate_limit_event` | — |
| 6 | `result` | `success` |
| 1 | `result` | `error_during_execution` |
| 1 | `result` | `error_max_budget_usd` |
| 1 | `control_response` | — |
| 26 | `stream_event` | (only with `--include-partial-messages`) |

`session_id` and `uuid` are on **every** line except `control_response`.

### `system` / `init`

`simple_edit.ndjson:1`, field inventory:

```
type  subtype  cwd  session_id  tools  mcp_servers  model  permissionMode
slash_commands  apiKeySource  claude_code_version  output_style  agents
skills  plugins  capabilities  analytics_disabled  product_feedback_disabled
uuid  memory_paths  fast_mode_state  fast_mode_disabled_reason
```

Values that matter: `cwd`, `session_id`, `model`, `permissionMode`,
`claude_code_version`, `capabilities`.

```json
{"type":"system","subtype":"init","cwd":"/tmp/repo","session_id":"b0f9cbf4-…","model":"claude-haiku-4-5","permissionMode":"bypassPermissions","apiKeySource":"none","claude_code_version":"2.1.222","capabilities":["interrupt_receipt_v1","interrupt_cancel_queued_v1","msg_lifecycle_v1"],…}
```

**`system/init` is emitted once per turn, not once per run.** `multi_turn.ndjson`
line 1 and line 13 are both `system/init` with the *same* `session_id`
(`f10abe4b-…`). A parser that maps `init → RunStarted` unconditionally will emit
two `RunStarted` for one run. See §8.1.

### `assistant`

```
type  message  parent_tool_use_id  session_id  uuid  timestamp  request_id
```

`message` is a verbatim Anthropic API message: `{model, id, type, role, content[],
stop_reason, stop_sequence, stop_details, usage, diagnostics, context_management}`.

**One API message is split across several `assistant` lines — one line per content
block — and each line repeats the identical `message.id` and the identical
`message.usage`.** In `simple_edit.ndjson` lines 7 and 8 are both
`msg_011CdkaUTKUGY1eGb5mQ2o8v` (line 7 = the `thinking` block, line 8 = the
`tool_use` block) and both carry `{"input_tokens":10,"cache_creation_input_tokens":3952,…}`.
This is the single biggest trap in the format — see §3.

Content block types seen: `thinking` (with a `signature` blob), `text`,
`tool_use`. `tool_use` blocks look like:

```json
{"type":"tool_use","id":"toolu_01BdF3Ujiaxy9kh7EMRk42Z4","name":"Write","input":{"file_path":"/tmp/repo/b.txt","content":"world"},"caller":{"type":"direct"}}
```

`parent_tool_use_id` is `null` at top level and set for sub-agent output.

### `user`

Tool results come back as synthetic `user` lines:

```
type  message  parent_tool_use_id  session_id  uuid  timestamp  tool_use_result  [tool_result_meta]
```

Success (`simple_edit.ndjson:12`) — note there is **no `is_error` key at all**,
so the test must be `not block.get("is_error", False)`:

```json
{"type":"user","message":{"role":"user","content":[{"tool_use_id":"toolu_01BdF3Ujiaxy9kh7EMRk42Z4","type":"tool_result","content":"File created successfully at: /tmp/repo/b.txt …"}]},…,"tool_use_result":{"type":"create","filePath":"/tmp/repo/b.txt","content":"world","structuredPatch":[],"originalFile":null,"userModified":false}}
```

Failure (`tool_error.ndjson:11`, missing file):

```json
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"File does not exist. Note: your current working directory is /tmp/repo.","is_error":true,"tool_use_id":"toolu_01QsXqRtRVjkEnMEbgbQE7VS"}]},…,"tool_use_result":"Error: File does not exist. Note: your current working directory is /tmp/repo."}
```

Failure (`tool_error.ndjson:13`, `exit 3`): `"content":"Exit code 3","is_error":true`,
`"tool_use_result":"Error: Exit code 3"`.

`tool_use_result` is **polymorphic**: a structured object on success, a plain
string on failure. Do not type it as `dict`.

A `user` line can also be plain injected/synthetic text with no tool result at
all — `interrupted.ndjson:19` is
`{"type":"user","message":{"role":"user","content":[{"type":"text","text":"[Request interrupted by user]"}]},…}`.
So `type == "user"` alone does not imply `ToolResult`; branch on the content
block type.

### `result`

Emitted **once per turn**, as the last line of that turn. Full non-usage field
inventory from `simple_edit.ndjson:14`:

```json
{"is_error":false,"duration_api_ms":6651,"num_turns":3,"stop_reason":"end_turn","session_id":"b0f9cbf4-…","total_cost_usd":0.0170877,"usage":{…},"modelUsage":{…},"permission_denials":[],"terminal_reason":"completed","fast_mode_state":"off","fast_mode_disabled_reason":"sdk_opt_in_required","subtype":"success","api_error_status":null,"result":"Done! I read a.txt (which contains \"hello\") and created b.txt with the single word \"world\".","ttft_ms":1877,"ttft_stream_ms":1171,"time_to_request_ms":34,"type":"result","duration_ms":5165,"uuid":"aef1ca85-…"}
```

Error variants:

- `budget_error.ndjson:12` — `"subtype":"error_max_budget_usd"`, `"is_error":true`,
  `"terminal_reason":"budget_exhausted"`, `"errors":["Reached maximum budget ($0.001)"]`,
  `"result"` key **absent**, process exit code **1**.
- `interrupted.ndjson:20` — `"subtype":"error_during_execution"`, `"is_error":true`,
  `"terminal_reason":"aborted_streaming"`, `"result":null`,
  `"errors":["[ede_diagnostic] result_type=user last_content_type=n/a stop_reason=tool_use"]`,
  process exit code **1**.

### `system` / `thinking_tokens`, `system` / `status`, `rate_limit_event`

```json
{"type":"system","subtype":"thinking_tokens","estimated_tokens":4,"estimated_tokens_delta":4,"uuid":"…","session_id":"…"}
{"type":"system","subtype":"status","status":"requesting","uuid":"…","session_id":"…"}
{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1785993600,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"out_of_credits","isUsingOverage":false},"uuid":"…","session_id":"…"}
```

`thinking_tokens` is a *client-side estimate* emitted while thinking streams — it
is **not** billing data and must never be added to the token totals.

### `control_response`

Only line in the fixtures without `session_id`/`uuid` (`interrupted.ndjson:17`):

```json
{"type":"control_response","response":{"subtype":"success","request_id":"req_1","response":{"still_queued":[]}}}
```

### `stream_event` (only with `--include-partial-messages`)

Wraps the raw Anthropic SSE event: `{"type":"stream_event","event":{…}}` with
`event.type` in `message_start`, `content_block_start`, `content_block_delta`,
`content_block_stop`, `message_delta`, `message_stop`. This is the only way to get
true incremental text and, critically, true output token counts (§3).

## 3. Is `message.usage` cumulative or incremental? — the answer

**Incremental per assistant message for the three *input-side* fields, and
`output_tokens` is not usable at all from `assistant` lines.**

Two separate traps, both proven below.

### Trap 1 — dedupe by `message.id` before summing

Because one API message becomes several `assistant` lines each repeating the same
usage block, summing over *lines* over-counts. `simple_edit.ndjson` has 5
`assistant` lines but only 3 distinct `message.id`s; summing lines gives
`input_tokens = 10+10+6+6+5 = 37`, summing distinct ids gives `21`, and the
`result` says `21`.

### Trap 2 — `output_tokens` on an `assistant` line is a streaming placeholder

Reconciliation across all four turns in the fixtures (sum over **distinct**
`message.id`, compared to that turn's `result.usage`):

| Fixture / turn | field | sum over distinct ids | `result.usage` | match |
|---|---|---|---|---|
| `simple_edit` | `input_tokens` | 21 | 21 | ✅ |
| | `cache_creation_input_tokens` | 6513 | 6513 | ✅ |
| | `cache_read_input_tokens` | 21737 | 21737 | ✅ |
| | `output_tokens` | **5** | **254** | ❌ |
| `tool_error` | `input_tokens` | 17 | 17 | ✅ |
| | `cache_creation_input_tokens` | 6561 | 6561 | ✅ |
| | `cache_read_input_tokens` | 12401 | 12401 | ✅ |
| | `output_tokens` | **5** | **261** | ❌ |
| `multi_turn` t1 | `input_tokens` | 15 | 15 | ✅ |
| | `cache_creation_input_tokens` | 6385 | 6385 | ✅ |
| | `cache_read_input_tokens` | 12380 | 12380 | ✅ |
| | `output_tokens` | **4** | **196** | ❌ |
| `multi_turn` t2 | `input_tokens` | 15 | 15 | ✅ |
| | `cache_creation_input_tokens` | 396 | 396 | ✅ |
| | `cache_read_input_tokens` | 18765 | 18765 | ✅ |
| | `output_tokens` | **5** | **230** | ❌ |

The three input-side fields reconcile **exactly**, in four independent turns. So
the interpretation is unambiguous: **incremental, one contribution per distinct
`message.id`, never cumulative.** (Cumulative would require the last message alone
to equal the total; in `simple_edit` the last message reports
`cache_read_input_tokens = 9338` against a total of `21737` — it does not.)

`output_tokens` is off by ~50×. Proof of *why*, from `partial_messages.ndjson`,
where the same API message appears both as an `assistant` line and as SSE events:

```
message_start   {"input_tokens":10,"cache_creation_input_tokens":3952,"cache_read_input_tokens":5472,…,"output_tokens":4,…}
assistant-line  {"input_tokens":10,"cache_creation_input_tokens":3952,"cache_read_input_tokens":5472,…,"output_tokens":4,…}
message_delta   {"input_tokens":10,"cache_creation_input_tokens":3952,"cache_read_input_tokens":5472,"output_tokens":260,"output_tokens_details":{"thinking_tokens":188},…}
```

The `assistant` line carries the **`message_start`** usage snapshot verbatim. The
real output count only arrives in `message_delta`. Summing the two `message_delta`
values in that fixture gives `260 + 15 = 275`, which is exactly the
`result.usage.output_tokens = 275`.

**Consequence for A5/A8.** Two workable strategies, pick one deliberately:

1. Emit `Usage` per distinct `message.id` with the three input-side fields, and
   take `output_tokens` from the per-turn `result` (one reconciling `Usage` at
   turn end, or correct the running total there). Totals are exact; output is
   only accurate at turn boundaries.
2. Pass `--include-partial-messages` and drive `Usage` off
   `stream_event/message_delta`, which has all four fields correct. Costs a much
   larger stream — 43 lines vs 14 for a comparable prompt.

What must **not** happen is summing `output_tokens` off `assistant` lines. That
under-reports output by roughly two orders of magnitude.

### `result.usage` is per-turn; `modelUsage` and `total_cost_usd` are cumulative

In `multi_turn.ndjson` the two `result` lines give:

| | turn 1 | turn 2 |
|---|---|---|
| `usage.input_tokens` | 15 | 15 |
| `usage.cache_read_input_tokens` | 12380 | 18765 |
| `modelUsage["claude-haiku-4-5"].inputTokens` | 15 | **30** |
| `modelUsage["claude-haiku-4-5"].cacheReadInputTokens` | 12380 | **31145** |
| `modelUsage["claude-haiku-4-5"].outputTokens` | 196 | **426** |
| `total_cost_usd` | 0.015601 | **0.0194345** |

`30 = 15+15`, `31145 = 12380+18765`, `426 = 196+230`. So `result.usage` resets
each turn while `modelUsage` and `total_cost_usd` accumulate over the session.
**Adding `total_cost_usd` across `result` lines double-counts.** Take the last one.

### There is token usage that never appears as an event

`modelUsage` always has two keys, and only one of them corresponds to the
conversation:

```json
"modelUsage":{
  "claude-haiku-4-5-20251001":{"inputTokens":532,"outputTokens":13,"cacheReadInputTokens":0,"cacheCreationInputTokens":0,"costUSD":0.000597,"canonicalModel":"claude-haiku-4-5","provider":"firstParty"},
  "claude-haiku-4-5":{"inputTokens":21,"outputTokens":254,"cacheReadInputTokens":21737,"cacheCreationInputTokens":6513,"costUSD":0.0164907,"canonicalModel":"claude-haiku-4-5","provider":"firstParty"}
}
```

The `claude-haiku-4-5` entry matches `result.usage` exactly. The
`claude-haiku-4-5-20251001` entry (532 in / 13 out) is a side-channel call — no
`assistant` line ever reports it — and it *is* included in `total_cost_usd`
(`0.000597 + 0.0164907 = 0.0170877`). Roughly ~530 input tokens per run of usage
that our `Usage` events cannot see. Our summed total will therefore always be
slightly below the CLI's `total_cost_usd`; the A8 test must compare against
`result.usage`, not against `total_cost_usd`.

Also note the key naming is inconsistent: the alias `claude-haiku-4-5` keys the
main conversation while `assistant` lines report `"model":"claude-haiku-4-5-20251001"`.
`canonicalModel` is the field that normalizes them, and it is what `pricing.yaml`
lookups should key on.

## 4. Exact token field names and the mapping to AgentHub

Inside `message.usage` and `result.usage`:

| Claude Code field | AgentHub field |
|---|---|
| `input_tokens` | `input_tokens` |
| `output_tokens` | `output_tokens` (see §3 — not from `assistant` lines) |
| `cache_read_input_tokens` | `cache_read_tokens` |
| `cache_creation_input_tokens` | `cache_write_tokens` |

Inside `result.modelUsage[*]` the same numbers appear camelCased —
`inputTokens`, `outputTokens`, `cacheReadInputTokens`, `cacheCreationInputTokens`.
Two spellings for the same four quantities in one file.

Present in the fixtures with **no home in AgentHub's four fields**:

- `cache_creation": {"ephemeral_5m_input_tokens":0,"ephemeral_1h_input_tokens":3952}` —
  the TTL split of `cache_creation_input_tokens`. `design.md` §4 prices 5 min at
  ~1.25× and 1 h at ~2.0×, so collapsing both into one `cache_write_tokens` makes
  cost wrong by up to 1.6× whenever the 1 h TTL is used. **Every capture here is
  100% `ephemeral_1h`** (`ephemeral_5m_input_tokens` is `0` in all of them), i.e.
  the expensive tier is the default in 2.1.222, not the exception. Flagging for A4:
  either split `cache_write_tokens` into two fields, or price all cache writes at
  the 1 h rate and document the choice.
- `service_tier`: `"standard"` — affects price on other tiers.
- `inference_geo`: `"not_available"` (`""` in the budget-error result).
- `output_tokens_details.thinking_tokens`: `188` (only in `message_delta`).
- `server_tool_use`: `{"web_search_requests":0,"web_fetch_requests":0}` — billed
  separately, not tokens.
- `speed`: `"standard"`; `iterations`: an array that, in the `result`, contains
  only the **last** message's usage, not every message.
- `message.diagnostics.cache_miss_reason`:
  `{"type":"messages_changed","cache_missed_input_tokens":2379}`.
- `modelUsage[*].contextWindow` / `maxOutputTokens` / `provider` / `webSearchRequests`.

## 5. Permissions: the `Permission` event does not exist on this channel

`permission_denied.ndjson` is the run with no `--permission-mode`. Claude Code
**never emits a permission request** on the stream. It silently refuses and
surfaces the refusal as a normal failed tool result (line 12):

```json
{"type":"user","message":{"role":"user","content":[{"type":"tool_result","content":"Claude requested permissions to write to /tmp/repo/b.txt, but you haven't granted it yet.","is_error":true,"tool_use_id":"toolu_01QMPgrSLhrY9eemkUdGSzYy"}]},…,"tool_result_meta":[{"id":"toolu_01QMPgrSLhrY9eemkUdGSzYy","non_execution_kind":"user-rejected"}]}
```

and retrospectively in the `result`:

```json
"permission_denials":[{"tool_name":"Write","tool_use_id":"toolu_01QMPgrSLhrY9eemkUdGSzYy","tool_input":{"file_path":"/tmp/repo/b.txt","content":"world"}}]
```

The run then finishes `"subtype":"success"`, `"is_error":false`, exit code **0**,
with `"result":"I need permission to write the file. Could you allow the write
operation to /tmp/repo/b.txt?"`. **A permission-blocked run looks like a
successful run.** `permission_denials` being non-empty is the only reliable
signal in the JSON.

`tool_result_meta[].non_execution_kind == "user-rejected"` is what distinguishes a
refusal from a genuine tool failure; both set `is_error: true`.

## 6. Does the `result` event carry status, exit code, cost and duration?

| Wanted | Present? | Field |
|---|---|---|
| status | yes | `subtype` (`success` / `error_max_budget_usd` / `error_during_execution`) + `is_error` + `terminal_reason` (`completed` / `budget_exhausted` / `aborted_streaming`) + `stop_reason` |
| **exit code** | **no** | nothing in the JSON. Observed out-of-band: `success` → 0, both error subtypes → 1. Must come from `Process.wait()`. |
| cost | yes | `total_cost_usd` — cumulative per session, includes the side-channel model (§3) |
| duration | yes | `duration_ms` (wall), `duration_api_ms` (API only), plus `ttft_ms`, `ttft_stream_ms`, `time_to_request_ms` |
| summary | yes-ish | `result` (string on success, `null` on abort, **key absent** on budget error) |

## 7. Message injection and interrupt on stdin — both work

`--input-format stream-json` was confirmed against `multi_turn.ndjson`. One JSON
object per line on stdin:

```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"…"}]}}
```

The turn boundary is a `result` line; writing the next message after it starts
turn 2 (a fresh `system/init` + a second `result`, same `session_id`). Closing
stdin ends the process cleanly with exit 0.

Interrupt also works over the same channel (`interrupted.ndjson`), which
`design.md` does not mention. Sending:

```json
{"type":"control_request","request_id":"req_1","request":{"subtype":"interrupt"}}
```

produces `{"type":"control_response","response":{"subtype":"success","request_id":"req_1","response":{"still_queued":[]}}}`,
then a `user` line `"[Request interrupted by user]"`, then a
`result`/`error_during_execution` with `terminal_reason: "aborted_streaming"`, and
the process exits **1**. This is much better than `os.killpg` for
`interrupt()` — it yields a parseable terminal event and correct final usage.
`system/init.capabilities` advertises `interrupt_receipt_v1` and
`interrupt_cancel_queued_v1`, so the parser can feature-detect it.

## 8. Divergences from `design.md` §3 / §4

Blunt list for A4 to decide on.

**8.1 `RunStarted` is not 1:1 with `system/init`.** `init` repeats at the start of
every turn with the same `session_id` (`multi_turn.ndjson` lines 1 and 13). Emit
`RunStarted` only for the first `init` of a session, or the dashboard shows one
run per turn. `RunStarted(run_id, harness, model, cwd, pid)`: `model`, `cwd`,
`session_id` and `claude_code_version` come from `init`; `pid` does not (it is
ours), and `run_id` is AgentHub's, not the CLI's `session_id` — carry both.

**8.2 `RunFinished` is not 1:1 with `result`.** `result` is per turn. In a
multi-turn session the run is not over when the first `result` lands. And
`RunFinished(status, exit_code, summary)` cannot be filled from JSON alone:
**there is no exit code in the stream** (§6).

**8.3 `Usage(input, output, cache_read, cache_write, model)` cannot be filled from
one `assistant` line.** `output` is a `message_start` placeholder, ~50× low (§3).
The event shape is fine; the parser must not populate it naively. Also needs
dedupe by `message.id`, since one message spans several lines.

**8.4 `cache_write_tokens` is under-specified.** `design.md` §4 prices 5 min and
1 h cache writes differently (1.25× vs 2.0×) but the event has one field. All
captures are 100% `ephemeral_1h`. Either split the field or fix the price and say
so.

**8.5 `Permission(request_id, description)` has no source.** In `-p
--output-format stream-json`, permission requests are never emitted; denials only
show up after the fact (§5). There is no `request_id` to answer. The human gate as
designed does not exist on Channel A in 2.1.222. Options: run with
`bypassPermissions` inside ai-jail and rely on the sandbox for safety (invariant
8), or investigate the MCP permission-prompt-tool route — but do not ship
`Permission` as if the CLI produced it.

**8.6 `ThinkingDelta(delta)` and `AssistantText(delta)` are not deltas.** Without
`--include-partial-messages`, `thinking` and `text` arrive as complete content
blocks on `assistant` lines. Either rename them (they are whole blocks) or make
`--include-partial-messages` mandatory and parse `stream_event`. Thinking blocks
also carry a `signature` blob that the event has nowhere to put.

**8.7 `design.md` §3 lists four line types; there are eleven.** Missing:
`stream_event`, `control_response`, `rate_limit_event`, `system/thinking_tokens`,
`system/status`. Each needs a deliberate map-or-drop.

**8.8 Cost is cumulative, not additive.** `total_cost_usd` accumulates across
`result` lines within a session (§3). Summing them double-counts. Independently,
`apiKeySource: "none"` confirms subscription auth, so invariant 7 applies to every
number here.

**8.9 Model ID has two spellings in one file.** `init.model` and
`modelUsage`'s main key use the alias `claude-haiku-4-5`; `assistant` lines report
`claude-haiku-4-5-20251001`. `pricing.yaml` lookups must normalize via
`modelUsage[*].canonicalModel`, or a dated ID will silently miss the price table
and leave `cost_usd` null.

**8.10 Things with no home in `AgentEvent` at all.** Listed so A4 can drop them on
purpose: `rate_limit_event.rate_limit_info` (subscription window, `resetsAt`,
`overageStatus` — arguably useful on the dashboard, but it is Anthropic-specific
and would violate invariant 1 if exposed as such), `system/thinking_tokens`
estimates, `system/status`, `permission_denials[]`, `ttft_ms` /
`ttft_stream_ms` / `time_to_request_ms`, `fast_mode_state` /
`fast_mode_disabled_reason`, `api_error_status`, `num_turns`, `stop_details`,
`context_management`, `diagnostics.cache_miss_reason`, `caller` on `tool_use`,
`message.signature` on thinking blocks, `errors[]` on error results, and the
whole `init` config inventory (`tools`, `skills`, `slash_commands`, `agents`,
`plugins`, `mcp_servers`, `output_style`, `memory_paths`).

## 9. Not captured

- Behaviour under `ai-jail` — deliberately out of scope here (A6).
- Behaviour on a PTY (Channel B) — Phase 1.
- Sub-agent output (`parent_tool_use_id` non-null) and `--forward-subagent-text`.
- MCP tool calls; the one configured MCP server was `needs-auth` throughout.
- Rate-limit exhaustion and API error results (`api_error_status` non-null).
