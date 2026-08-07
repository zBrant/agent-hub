/**
 * The only place the `/ws` wire format is written down.
 *
 * B6 (WebSocket event broker) owns this format: topic multiplexing over a
 * single connection (docs/architecture.md §6), `<resource>:<id>` topic names,
 * and stream-scoped cursors for bounded replay after a reconnect.
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

/** A durable event delivered on one multiplexed topic. */
export type EventFrame = {
  type: "event";
  stream: string;
  topic: Topic;
  seq: number;
  payload: TopicPayload;
};

export type ReadyFrame = {
  type: "ready";
  stream: string;
  topic: Topic;
  cursor: number;
};

export type ErrorFrame = {
  type: "error";
  code: "invalid_frame" | "history_gap";
  message: string;
};

export type ServerFrame = EventFrame | ReadyFrame | ErrorFrame;

/** Control frames the client sends. A cursor is valid only in its stream. */
export type ClientFrame =
  | { type: "subscribe"; topic: Topic; stream?: string; after?: number }
  | { type: "unsubscribe"; topic: Topic };

export function encodeClientFrame(frame: ClientFrame): string {
  return JSON.stringify(frame);
}

function isTopic(value: unknown): value is Topic {
  return (
    typeof value === "string" &&
    (value === METRICS_TOPIC ||
      (value.startsWith("session:") && value.length > "session:".length) ||
      (value.startsWith("run:") && value.length > "run:".length))
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
  if (frame.type === "event") {
    if (
      !isTopic(frame.topic) ||
      typeof frame.stream !== "string" ||
      frame.stream.length === 0 ||
      typeof frame.seq !== "number" ||
      !Number.isSafeInteger(frame.seq) ||
      frame.seq < 1
    ) {
      return null;
    }
    return {
      type: "event",
      stream: frame.stream,
      topic: frame.topic,
      seq: frame.seq,
      payload: frame.payload,
    };
  }
  if (frame.type === "ready") {
    if (
      !isTopic(frame.topic) ||
      typeof frame.stream !== "string" ||
      frame.stream.length === 0 ||
      typeof frame.cursor !== "number" ||
      !Number.isSafeInteger(frame.cursor) ||
      frame.cursor < 0
    ) {
      return null;
    }
    return {
      type: "ready",
      stream: frame.stream,
      topic: frame.topic,
      cursor: frame.cursor,
    };
  }
  if (
    frame.type === "error" &&
    (frame.code === "invalid_frame" || frame.code === "history_gap") &&
    typeof frame.message === "string"
  ) {
    return { type: "error", code: frame.code, message: frame.message };
  }
  return null;
}
