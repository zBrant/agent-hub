import { beforeEach, describe, expect, it } from "vitest";
import type { AssistantText, RunFinished, Usage } from "@/api/events";
import { useSessionFeedStore } from "@/stores/session-feed-store";
import { useSystemMetricsStore } from "@/stores/system-metrics-store";
import { decodeServerFrame } from "@/ws/protocol";

function text(runId: string, ts: number, value: string): AssistantText {
  return { type: "assistant_text", run_id: runId, ts, text: value };
}

describe("session feed persistence and live overlap", () => {
  beforeEach(() => {
    useSessionFeedStore.setState({ eventsByRun: {} });
  });

  it("hydrates persisted facts and removes only the REST/WS overlap", () => {
    const first = text("run_one", 1, "first");
    const second = text("run_one", 2, "second");
    const finished: RunFinished = {
      type: "run_finished",
      run_id: "run_one",
      ts: 3,
      status: "success",
      exit_code: 0,
    };

    useSessionFeedStore.getState().append(second);
    useSessionFeedStore.getState().append(finished);
    useSessionFeedStore.getState().hydrate("run_one", [first, second]);

    expect(useSessionFeedStore.getState().eventsByRun.run_one).toEqual([
      first,
      second,
      finished,
    ]);
  });

  it("preserves two genuinely identical persisted event records", () => {
    const usage: Usage = {
      type: "usage",
      run_id: "run_one",
      ts: 5,
      model: "model",
      input_tokens: 1,
    };
    useSessionFeedStore.getState().hydrate("run_one", [usage, usage]);
    expect(useSessionFeedStore.getState().eventsByRun.run_one).toHaveLength(2);
  });
});

describe("WebSocket AgentEvent validation", () => {
  it("accepts a canonical generated event and rejects a malformed payload", () => {
    const valid = JSON.stringify({
      type: "event",
      stream: "broker",
      topic: "run:run_one",
      seq: 1,
      payload: text("run_one", 1, "hello"),
    });
    expect(decodeServerFrame(valid)?.type).toBe("event");

    const malformed = JSON.stringify({
      type: "event",
      stream: "broker",
      topic: "run:run_one",
      seq: 2,
      payload: { type: "assistant_text", text: "missing identity" },
    });
    expect(decodeServerFrame(malformed)).toBeNull();
  });

  it("decodes graph node transitions as orchestration state", () => {
    const frame = decodeServerFrame(
      JSON.stringify({
        type: "node_status",
        stream: "stream_one",
        topic: "graph:sess_one",
        seq: 4,
        payload: {
          session_id: "sess_one",
          node_id: "node_one",
          status: "running",
          ts: 20,
        },
      }),
    );

    expect(frame?.type).toBe("node_status");
    if (frame?.type === "node_status") {
      expect(frame.payload.status).toBe("running");
      expect(frame.topic).toBe("graph:sess_one");
    }
  });

  it("decodes generated system metrics and rejects an incomplete snapshot", () => {
    const payload = {
      ts: 20,
      cpu_percent: 34.5,
      cpu_per_core: [20, 49],
      memory_total_bytes: 1_000,
      memory_used_bytes: 600,
      memory_available_bytes: 400,
      memory_percent: 60,
      swap_total_bytes: 500,
      swap_used_bytes: 100,
      swap_free_bytes: 400,
      swap_percent: 20,
      disk_total_bytes: 2_000,
      disk_used_bytes: 500,
      disk_free_bytes: 1_500,
      disk_percent: 25,
      processes: [
        {
          node_id: "node_one",
          pid: 123,
          harness: "codex",
          rss_bytes: 256,
          cpu_percent: 12.5,
          uptime_ms: 5_000,
          process_count: 2,
        },
      ],
    };
    const frame = decodeServerFrame(
      JSON.stringify({
        type: "metrics",
        stream: "stream_one",
        topic: "metrics",
        seq: 5,
        payload,
      }),
    );

    expect(frame?.type).toBe("metrics");
    if (frame?.type === "metrics") {
      expect(frame.payload.processes[0]?.node_id).toBe("node_one");
    }
    expect(
      decodeServerFrame(
        JSON.stringify({
          type: "metrics",
          stream: "stream_one",
          topic: "metrics",
          seq: 6,
          payload: { ts: 21, cpu_percent: 4 },
        }),
      ),
    ).toBeNull();
  });
});

describe("system metrics live store", () => {
  beforeEach(() => useSystemMetricsStore.getState().reset());

  it("retains 300 samples and replaces a duplicate current snapshot", () => {
    const base = {
      cpu_percent: 1,
      cpu_per_core: [1],
      memory_total_bytes: 1,
      memory_used_bytes: 1,
      memory_available_bytes: 0,
      memory_percent: 100,
      swap_total_bytes: 0,
      swap_used_bytes: 0,
      swap_free_bytes: 0,
      swap_percent: 0,
      disk_total_bytes: 1,
      disk_used_bytes: 1,
      disk_free_bytes: 0,
      disk_percent: 100,
      processes: [],
    } as const;
    const store = useSystemMetricsStore.getState();
    for (let ts = 1; ts <= 301; ts += 1) store.push({ ...base, ts });
    store.push({ ...base, ts: 301, cpu_percent: 2 });

    const state = useSystemMetricsStore.getState();
    expect(state.samples).toHaveLength(300);
    expect(state.samples[0]?.ts).toBe(2);
    expect(state.latest?.cpu_percent).toBe(2);
  });
});
