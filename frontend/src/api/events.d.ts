/**
 * GENERATED FILE — do not edit.
 * Run `pnpm gen:api`. See src/api/README.md and docs/architecture.md §7.
 */

export type AgentEvent =
  | RunStarted
  | TurnStarted
  | AssistantText
  | ThinkingDelta
  | ToolCall
  | ToolResult
  | Usage
  | Permission
  | TurnFinished
  | RunFinished
  | RawChunk;
export type Cwd = string;
export type Harness = string;
export type HarnessVersion = string | null;
export type Model = string;
export type Pid = number | null;
export type RunId = string;
export type SessionId = string | null;
export type Ts = number;
export type Type = "run_started";
export type Model1 = string;
export type SessionId1 = string | null;
export type Ts1 = number;
export type Turn = number;
export type Type1 = "turn_started";
export type Text = string;
export type Ts2 = number;
export type Type2 = "assistant_text";
export type Text1 = string;
export type Ts3 = number;
export type Type3 = "thinking_delta";
export type CallId = string;
export type Tool = string;
export type Ts4 = number;
export type Type4 = "tool_call";
export type CallId1 = string;
export type Denied = boolean;
export type Ok = boolean;
export type Preview = string;
export type Ts5 = number;
export type Type5 = "tool_result";
export type CacheReadTokens = number;
export type CacheWrite1HTokens = number;
export type CacheWrite5MTokens = number;
export type CacheWriteTokens = number;
export type InputTokens = number;
export type Model2 = string;
export type OutputTokens = number;
export type Ts6 = number;
export type Type6 = "usage";
export type Description = string;
export type RequestId = string;
export type Ts7 = number;
export type Type7 = "permission";
export type DurationMs = number | null;
export type Errors = string[];
export type CallId2 = string | null;
export type Tool1 = string;
export type PermissionDenials = PermissionDenial[];
export type SessionId2 = string | null;
export type RunStatus = "success" | "failed" | "interrupted" | "budget_exceeded";
export type Summary = string | null;
export type Ts8 = number;
export type Turn1 = number;
export type Type8 = "turn_finished";
export type ExitCode = number | null;
export type Summary1 = string | null;
export type Ts9 = number;
export type Type9 = "run_finished";
export type Data = string;
export type Ts10 = number;
export type Type10 = "raw_chunk";

/**
 * The process is up. Exactly one per run, whatever the harness repeats.
 *
 * ``session_id`` is the *harness's* own identifier and ``run_id`` is ours —
 * both are kept because the harness id is what you need to resume or to
 * correlate with the CLI's own logs, and ours is what the database keys on.
 */
export interface RunStarted {
  cwd: Cwd;
  harness: Harness;
  harness_version?: HarnessVersion;
  model: Model;
  pid?: Pid;
  run_id: RunId;
  session_id?: SessionId;
  ts: Ts;
  type?: Type;
}
/**
 * One request/response cycle begins. ``turn`` is 1-based and per run.
 */
export interface TurnStarted {
  model: Model1;
  run_id: RunId;
  session_id?: SessionId1;
  ts: Ts1;
  turn: Turn;
  type?: Type1;
}
/**
 * Assistant-visible text.
 *
 * `design.md` §3 calls this ``AssistantText(delta)``. It is **not** a delta in
 * Claude Code 2.1.222 without ``--include-partial-messages``: text arrives as
 * a complete content block on an ``assistant`` line. The field is therefore
 * ``text``, and the contract for consumers is *append* — which is correct for
 * both whole blocks and the true deltas Phase 1 will emit from
 * ``stream_event/content_block_delta``. No consumer needs to know which it got.
 */
export interface AssistantText {
  run_id: RunId;
  text: Text;
  ts: Ts2;
  type?: Type2;
}
/**
 * Extended-thinking text. Same whole-block caveat as :class:`AssistantText`.
 *
 * The class name is `design.md` §3's; the payload field is ``text`` for the
 * reason above. The provider's ``signature`` blob on a thinking block is
 * dropped — it is an opaque re-submission token for the API, has no meaning to
 * a dashboard, and is large.
 */
export interface ThinkingDelta {
  run_id: RunId;
  text: Text1;
  ts: Ts3;
  type?: Type3;
}
/**
 * The agent invoked a tool. ``call_id`` correlates with :class:`ToolResult`.
 */
export interface ToolCall {
  call_id: CallId;
  input?: Input;
  run_id: RunId;
  tool: Tool;
  ts: Ts4;
  type?: Type4;
}
export interface Input {
  [k: string]: unknown;
}
/**
 * The outcome of a :class:`ToolCall`.
 *
 * ``denied`` separates *the sandbox or the permission layer refused this* from
 * *the tool ran and failed*. Both arrive with ``ok=False`` and they mean very
 * different things to the operator: a denial is a policy problem, an error is
 * the agent's problem. Claude Code marks the first with
 * ``tool_result_meta[].non_execution_kind == "user-rejected"``.
 */
export interface ToolResult {
  call_id: CallId1;
  denied?: Denied;
  ok: Ok;
  preview?: Preview;
  run_id: RunId;
  ts: Ts5;
  type?: Type5;
}
/**
 * Token consumption. The four fields of invariant 3, plus the tier split.
 *
 * ``input_tokens + output_tokens + cache_read_tokens + cache_write_tokens`` is
 * the contract; summing only ``input_tokens`` makes a long session look ~100x
 * cheaper than it was.
 *
 * ``cache_write_5m_tokens`` / ``cache_write_1h_tokens`` are a **breakdown of**
 * ``cache_write_tokens``, not additions to it, and the validator below enforces
 * that. They exist because `design.md` §4 prices the two TTLs differently
 * (~1.25x vs ~2.0x input), so collapsing them is up to a 1.6x cost error — and
 * every A3 capture is 100% ``ephemeral_1h``, i.e. the expensive tier is the
 * default in Claude Code 2.1.222, not the exception. Only pricing reads them;
 * the four-field contract is unchanged. Both default to 0 for harnesses that
 * do not report a split.
 *
 * ``model`` is the raw string the harness reported, never normalized here —
 * Claude Code spells the same model two ways (``claude-haiku-4-5`` on
 * ``system/init``, ``claude-haiku-4-5-20251001`` on ``assistant`` lines) and
 * deciding which one prices correctly belongs to the pricing table, not to a
 * parser that would have to throw one of them away.
 *
 * ``source`` says whether the harness *reported* these numbers or the adapter
 * *reconstructed* them from a second, cumulative accounting the harness also
 * publishes. A reconstruction is exact where it has been validated and is
 * still a derivation — a dashboard should be able to say so, and a
 * reconciliation bug should be attributable. It stays deliberately generic:
 * which field it was rebuilt from is the adapter's business (invariant 1).
 */
export interface Usage {
  cache_read_tokens?: CacheReadTokens;
  cache_write_1h_tokens?: CacheWrite1HTokens;
  cache_write_5m_tokens?: CacheWrite5MTokens;
  cache_write_tokens?: CacheWriteTokens;
  input_tokens?: InputTokens;
  model: Model2;
  output_tokens?: OutputTokens;
  run_id: RunId;
  source?: "reported" | "reconstructed";
  ts: Ts6;
  type?: Type6;
}
/**
 * A human gate: the agent is asking to do something and is blocked on it.
 *
 * **Channel A never produces this in ``-p`` mode.** Verified against Claude
 * Code 2.1.222: with no ``--permission-mode`` the CLI does not emit a request
 * at all — it refuses silently, the refusal surfaces as an ordinary
 * ``is_error`` tool result, and the run still reports success with exit 0. The
 * only trace is ``result.permission_denials[]``, which lands on
 * :class:`TurnFinished` *after the fact*. There is no ``request_id`` to answer.
 *
 * The variant stays declared because `design.md` §3 specifies it and other
 * harnesses (or an MCP permission-prompt tool) may yet produce it. Nothing in
 * Phase 0 emits it; do not build a UI that waits for one.
 */
export interface Permission {
  description: Description;
  request_id: RequestId;
  run_id: RunId;
  ts: Ts7;
  type?: Type7;
}
/**
 * One request/response cycle ended.
 *
 * **Read ``permission_denials`` before you mark anything done.** A run whose
 * every write was refused is reported by Claude Code 2.1.222 as
 * ``subtype: "success"``, ``is_error: false``, exit code 0 — indistinguishable
 * from a run that did the work. ``status`` will say ``"success"`` because that
 * is what the harness said; the non-empty ``permission_denials`` tuple is the
 * only signal in the data, and :attr:`blocked_by_permission` is how the
 * scheduler must consume it. A node that transitions to *done* while
 * ``blocked_by_permission`` is true has reported a lie to the user and merged
 * an empty diff.
 *
 * ``errors`` carries the harness's own failure strings. They are generalized
 * rather than dropped because a failed turn with no explanation is
 * unactionable for the operator.
 *
 * The harness's self-reported cost is deliberately **not** here: Claude Code's
 * ``total_cost_usd`` accumulates across turns (summing it double-counts) and
 * includes side-channel model calls no event can see. Cost is computed at
 * ingest from :class:`Usage` against the pricing table (invariant 3), and is
 * labelled *estimated equivalent* under a subscription (invariant 7).
 */
export interface TurnFinished {
  duration_ms?: DurationMs;
  errors?: Errors;
  permission_denials?: PermissionDenials;
  run_id: RunId;
  session_id?: SessionId2;
  status: RunStatus;
  summary?: Summary;
  ts: Ts8;
  turn: Turn1;
  type?: Type8;
}
/**
 * One tool call the permission layer refused. See :class:`TurnFinished`.
 */
export interface PermissionDenial {
  call_id?: CallId2;
  input?: Input1;
  tool: Tool1;
}
export interface Input1 {
  [k: string]: unknown;
}
/**
 * The process exited. Exactly one per run, and it is the last event.
 *
 * ``exit_code`` has **no source in any harness's JSON** — Claude Code's
 * ``result`` line carries a status but not the code. The adapter synthesizes
 * this event from ``Process.wait()``, so a parser working on a recorded stream
 * (replay, fixtures) legitimately produces none.
 */
export interface RunFinished {
  exit_code?: ExitCode;
  run_id: RunId;
  status: RunStatus;
  summary?: Summary1;
  ts: Ts9;
  type?: Type9;
}
/**
 * Channel B only: raw PTY bytes for xterm.js.
 *
 * Never derive state from this (`docs/architecture.md` §5) — it is pixels,
 * including ANSI cursor addressing and alternate-screen switches. Base64 on
 * the wire because PTY output is not valid UTF-8 in general.
 */
export interface RawChunk {
  data: Data;
  run_id: RunId;
  ts: Ts10;
  type?: Type10;
}
