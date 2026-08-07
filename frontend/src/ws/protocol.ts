/**
 * The only place the `/ws` wire format is written down.
 *
 * B6 (WebSocket event broker) owns the real format. Everything here is either
 * fixed by an already-written decision — topic multiplexing over a single
 * connection (docs/architecture.md §6) and the `<resource>:<id>` topic naming
 * (docs/conventions.md §4) — or is an explicitly marked seam. When B6 lands,
 * aligning the client is a change to this file and nothing else.
 */

export type SessionTopic = `session:${string}`;
export type RunTopic = `run:${string}`;
export type MetricsTopic = "metrics";

/** WS topic vocabulary — docs/conventions.md §4. */
export type Topic = SessionTopic | RunTopic | MetricsTopic;

export function sessionTopic(sessionId: string): SessionTopic {
  return `session:${sessionId}`;
}

export function runTopic(runId: string): RunTopic {
  return `run:${runId}`;
}

export const METRICS_TOPIC: MetricsTopic = "metrics";

/**
 * SEAM — the payload the broker publishes on a topic.
 *
 * It is `unknown` on purpose. `AgentEvent` has no TypeScript form yet:
 * `src/api/events.d.ts` is generated from the canonical Pydantic schema and
 * docs/architecture.md §7 forbids hand-writing a mirror of a Python model.
 *
 * B9 replaces this single alias with the generated union:
 *
 *     import type { AgentEvent } from "@/api/events";
 *     export type TopicPayload = AgentEvent;
 *
 * Widening it is source-compatible for every subscriber, because `unknown`
 * forces them to narrow today. The one place the compiler will complain is
 * `decodeServerFrame` below — which is exactly where the runtime validation
 * belongs.
 */
export type TopicPayload = unknown;

/** A frame the broker sends. Only the envelope is fixed here; see TopicPayload. */
export type ServerFrame = {
  topic: Topic;
  payload: TopicPayload;
};

/** Control frames the client sends. Subscribe/unsubscribe is B8's mandate. */
export type ClientFrame =
  | { type: "subscribe"; topic: Topic }
  | { type: "unsubscribe"; topic: Topic };

export function encodeClientFrame(frame: ClientFrame): string {
  return JSON.stringify(frame);
}

function isTopic(value: unknown): value is Topic {
  return (
    typeof value === "string" &&
    (value === METRICS_TOPIC ||
      value.startsWith("session:") ||
      value.startsWith("run:"))
  );
}

/**
 * Parse one raw frame. Returns `null` for anything unrecognizable — a malformed
 * frame must never take the connection down, and it must never be silently
 * turned into a half-built object either.
 *
 * B9: once `TopicPayload` is `AgentEvent`, validate `payload` here (the
 * discriminated union's `type` field) before returning it.
 */
export function decodeServerFrame(raw: unknown): ServerFrame | null {
  if (typeof raw !== "string") return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }

  if (typeof parsed !== "object" || parsed === null) return null;
  const frame = parsed as Record<string, unknown>;
  if (!isTopic(frame.topic)) return null;

  return { topic: frame.topic, payload: frame.payload };
}
