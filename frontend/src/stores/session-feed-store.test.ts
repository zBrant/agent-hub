import { beforeEach, describe, expect, it } from "vitest";
import type { AssistantText, RunFinished, Usage } from "@/api/events";
import { useSessionFeedStore } from "@/stores/session-feed-store";
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
});
