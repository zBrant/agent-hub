/**
 * The only place the `/ws` wire format is written down.
 *
 * B6 (WebSocket event broker) owns this format: topic multiplexing over a
 * single connection (docs/architecture.md §6), `<resource>:<id>` topic names,
 * and stream-scoped cursors for bounded replay after a reconnect.
 */

import type { AgentEvent } from "@/api/events";
import type { components } from "@/api/schema";
import { NODE_STATES } from "@/lib/node-state";

export type SessionTopic = `session:${string}`;
export type RunTopic = `run:${string}`;
export type GraphTopic = `graph:${string}`;
export type MetricsTopic = "metrics";

/** WS topic vocabulary — docs/conventions.md §4. */
export type Topic = SessionTopic | RunTopic | GraphTopic | MetricsTopic;

export function sessionTopic(sessionId: string): SessionTopic {
  return `session:${sessionId}`;
}

export function runTopic(runId: string): RunTopic {
  return `run:${runId}`;
}

export function graphTopic(sessionId: string): GraphTopic {
  return `graph:${sessionId}`;
}

export const METRICS_TOPIC: MetricsTopic = "metrics";

/**
 * The payload comes from the generated canonical Pydantic schema. Runtime
 * validation below checks the stable envelope/discriminator fields; component
 * code receives the generated union and narrows on `type`.
 */
export type NodeStatusPayload = {
  session_id: string;
  node_id: string;
  status: components["schemas"]["NodeStatus"];
  ts: number;
};

export type SystemMetricsPayload =
  components["schemas"]["SystemSnapshotResponse"];

export type TopicPayload =
  | AgentEvent
  | NodeStatusPayload
  | SystemMetricsPayload;

/** A durable event delivered on one multiplexed topic. */
export type EventFrame = {
  type: "event";
  stream: string;
  topic: Topic;
  seq: number;
  payload: AgentEvent;
};

export type NodeStatusFrame = {
  type: "node_status";
  stream: string;
  topic: GraphTopic;
  seq: number;
  payload: NodeStatusPayload;
};

export type MetricsFrame = {
  type: "metrics";
  stream: string;
  topic: MetricsTopic;
  seq: number;
  payload: SystemMetricsPayload;
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

export type ServerFrame =
  | EventFrame
  | NodeStatusFrame
  | MetricsFrame
  | ReadyFrame
  | ErrorFrame;

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
      (value.startsWith("run:") && value.length > "run:".length) ||
      (value.startsWith("graph:") && value.length > "graph:".length))
  );
}

const EVENT_TYPES = new Set([
  "run_started",
  "turn_started",
  "assistant_text",
  "thinking_delta",
  "tool_call",
  "tool_result",
  "usage",
  "permission",
  "turn_finished",
  "run_finished",
  "raw_chunk",
]);

function isAgentEvent(value: unknown): value is AgentEvent {
  if (typeof value !== "object" || value === null) return false;
  const event = value as Record<string, unknown>;
  return (
    typeof event.type === "string" &&
    EVENT_TYPES.has(event.type) &&
    typeof event.run_id === "string" &&
    typeof event.ts === "number" &&
    Number.isSafeInteger(event.ts)
  );
}

const NODE_STATUSES = new Set<string>(NODE_STATES);

function isNodeStatusPayload(value: unknown): value is NodeStatusPayload {
  if (typeof value !== "object" || value === null) return false;
  const payload = value as Record<string, unknown>;
  return (
    typeof payload.session_id === "string" &&
    typeof payload.node_id === "string" &&
    typeof payload.status === "string" &&
    NODE_STATUSES.has(payload.status) &&
    typeof payload.ts === "number" &&
    Number.isSafeInteger(payload.ts)
  );
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isAgentProcessMetric(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  const process = value as Record<string, unknown>;
  return (
    typeof process.node_id === "string" &&
    isNonNegativeInteger(process.pid) &&
    typeof process.harness === "string" &&
    isNonNegativeInteger(process.rss_bytes) &&
    isFiniteNumber(process.cpu_percent) &&
    isNonNegativeInteger(process.uptime_ms) &&
    isNonNegativeInteger(process.process_count)
  );
}

function isSystemMetricsPayload(value: unknown): value is SystemMetricsPayload {
  if (typeof value !== "object" || value === null) return false;
  const snapshot = value as Record<string, unknown>;
  return (
    isNonNegativeInteger(snapshot.ts) &&
    isFiniteNumber(snapshot.cpu_percent) &&
    Array.isArray(snapshot.cpu_per_core) &&
    snapshot.cpu_per_core.every(isFiniteNumber) &&
    isNonNegativeInteger(snapshot.memory_total_bytes) &&
    isNonNegativeInteger(snapshot.memory_used_bytes) &&
    isNonNegativeInteger(snapshot.memory_available_bytes) &&
    isFiniteNumber(snapshot.memory_percent) &&
    isNonNegativeInteger(snapshot.swap_total_bytes) &&
    isNonNegativeInteger(snapshot.swap_used_bytes) &&
    isNonNegativeInteger(snapshot.swap_free_bytes) &&
    isFiniteNumber(snapshot.swap_percent) &&
    isNonNegativeInteger(snapshot.disk_total_bytes) &&
    isNonNegativeInteger(snapshot.disk_used_bytes) &&
    isNonNegativeInteger(snapshot.disk_free_bytes) &&
    isFiniteNumber(snapshot.disk_percent) &&
    Array.isArray(snapshot.processes) &&
    snapshot.processes.every(isAgentProcessMetric)
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
  if (frame.type === "metrics") {
    if (
      frame.topic !== METRICS_TOPIC ||
      typeof frame.stream !== "string" ||
      frame.stream.length === 0 ||
      typeof frame.seq !== "number" ||
      !Number.isSafeInteger(frame.seq) ||
      frame.seq < 1 ||
      !isSystemMetricsPayload(frame.payload)
    ) {
      return null;
    }
    return {
      type: "metrics",
      stream: frame.stream,
      topic: frame.topic,
      seq: frame.seq,
      payload: frame.payload,
    };
  }
  if (frame.type === "node_status") {
    if (
      typeof frame.topic !== "string" ||
      !frame.topic.startsWith("graph:") ||
      frame.topic.length === "graph:".length ||
      typeof frame.stream !== "string" ||
      frame.stream.length === 0 ||
      typeof frame.seq !== "number" ||
      !Number.isSafeInteger(frame.seq) ||
      frame.seq < 1 ||
      !isNodeStatusPayload(frame.payload)
    ) {
      return null;
    }
    return {
      type: "node_status",
      stream: frame.stream,
      topic: frame.topic as GraphTopic,
      seq: frame.seq,
      payload: frame.payload,
    };
  }
  if (frame.type === "event") {
    if (
      !isTopic(frame.topic) ||
      typeof frame.stream !== "string" ||
      frame.stream.length === 0 ||
      typeof frame.seq !== "number" ||
      !Number.isSafeInteger(frame.seq) ||
      frame.seq < 1 ||
      !isAgentEvent(frame.payload)
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
