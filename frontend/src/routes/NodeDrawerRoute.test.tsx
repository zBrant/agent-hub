// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Node, Run } from "@/api/client";
import type { AgentEvent } from "@/api/events";
import { NodeDrawerRoute } from "@/routes/NodeDrawerRoute";
import { useSessionFeedStore } from "@/stores/session-feed-store";
import type { EventFrame, NodeStatusFrame, TopicPayload } from "@/ws/protocol";

type Handler = (
  payload: TopicPayload,
  frame: EventFrame | NodeStatusFrame,
) => void;

const harness = vi.hoisted(() => ({
  handlers: new Map<string, Handler>(),
  listRuns: vi.fn(),
  summary: vi.fn(),
  events: vi.fn(),
  diff: vi.fn(),
  acceptance: vi.fn(),
  runGraph: vi.fn(),
  kill: vi.fn(),
  retry: vi.fn(),
  approve: vi.fn(),
  reject: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    listNodeRuns: harness.listRuns,
    getNodeRunSummary: harness.summary,
    getNodeRunEvents: harness.events,
    getNodeDiff: harness.diff,
    listNodeAcceptance: harness.acceptance,
    runGraph: harness.runGraph,
    killNode: harness.kill,
    retryNode: harness.retry,
    approveNode: harness.approve,
    rejectNode: harness.reject,
  },
}));

vi.mock("@/ws/WebSocketProvider", () => ({
  useWebSocketClient: () => ({
    subscribe: (topic: string, handler: Handler) => {
      harness.handlers.set(topic, handler);
      return () => harness.handlers.delete(topic);
    },
  }),
}));

const node: Node = {
  id: "node_one",
  session_id: "sess_one",
  name: "Live node",
  prompt: "Build it",
  acceptance_criteria: [],
  harness: "codex",
  model: "gpt-5.6-terra",
  touches: [],
  estimated_effort: null,
  worktree_path: "/workspace/node_one",
  branch: "agenthub/node_one",
  base_ref: "abc",
  status: "running",
  created_ms: 1,
  updated_ms: 1,
};

const run: Run = {
  id: "run_one",
  node_id: node.id,
  session_id: node.session_id,
  attempt: 1,
  status: "running",
  harness: "codex",
  model: "gpt-5.6-terra",
  pid: 42,
  harness_session_id: "thread",
  harness_version: "1",
  started_ms: 1,
  finished_ms: null,
  exit_code: null,
  summary: null,
  event_count: 1,
  permission_denial_count: 0,
  created_ms: 1,
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("node drawer route", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    harness.handlers.clear();
    useSessionFeedStore.setState({ eventsByRun: {} });
    harness.listRuns.mockResolvedValue([run]);
    harness.summary.mockResolvedValue({
      run_id: run.id,
      trusted: true,
      tokens: {
        input_tokens: 0,
        output_tokens: 0,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        total_tokens: 0,
      },
      estimated_equivalent_cost_usd: 0,
      cost_complete: true,
    });
    harness.events.mockResolvedValue([]);
    harness.diff.mockResolvedValue({ patch: "" });
    harness.acceptance.mockResolvedValue([]);
    harness.runGraph.mockResolvedValue({});
    harness.kill.mockResolvedValue(run);
    harness.retry.mockResolvedValue({});
    harness.approve.mockResolvedValue({
      status: "merged",
      commit: "abc",
      conflicts: [],
    });
    harness.reject.mockResolvedValue({
      node_id: node.id,
      attempt: 1,
      decision: "rejected",
      feedback: "Fix it",
      reviewed_ms: 2,
    });
  });

  afterEach(cleanup);

  it("streams the selected node's run topic and exposes kill", async () => {
    render(
      <NodeDrawerRoute
        dependencies={[]}
        node={node}
        onClose={vi.fn()}
        onDeleteNode={vi.fn()}
        onUpdateNode={vi.fn()}
        sessionId="sess_one"
      />,
      { wrapper },
    );

    await waitFor(() => expect(harness.handlers.has("run:run_one")).toBe(true));
    const handler = harness.handlers.get("run:run_one");
    const event: AgentEvent = {
      type: "assistant_text",
      run_id: "run_one",
      ts: 2,
      text: "Live from the selected node",
    };
    act(() =>
      handler?.(event, {
        type: "event",
        stream: "stream_one",
        topic: "run:run_one",
        seq: 1,
        payload: event,
      }),
    );

    expect(await screen.findByText("Live from the selected node")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Kill" }));
    await waitFor(() =>
      expect(harness.kill).toHaveBeenCalledWith("sess_one", "node_one"),
    );
  });

  it("sends criterion outcomes with both human review actions", async () => {
    harness.diff.mockResolvedValue({ patch: "diff --git a/a.py b/a.py" });
    harness.acceptance.mockResolvedValue([
      {
        node_id: node.id,
        attempt: 1,
        position: 0,
        criterion: "Tests pass",
        outcome: "unevaluated",
        created_ms: 1,
        updated_ms: 1,
      },
    ]);
    render(
      <NodeDrawerRoute
        dependencies={[]}
        node={{ ...node, status: "awaiting_review" }}
        onClose={vi.fn()}
        onDeleteNode={vi.fn()}
        onUpdateNode={vi.fn()}
        sessionId="sess_one"
      />,
      { wrapper },
    );

    fireEvent.change(await screen.findByLabelText("Outcome for Tests pass"), {
      target: { value: "pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve merge" }));
    await waitFor(() =>
      expect(harness.approve).toHaveBeenCalledWith("sess_one", "node_one", {
        0: "pass",
      }),
    );

    fireEvent.change(screen.getByLabelText("Rejection feedback"), {
      target: { value: "Fix it" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject and retry" }));
    await waitFor(() =>
      expect(harness.reject).toHaveBeenCalledWith(
        "sess_one",
        "node_one",
        "Fix it",
        { 0: "pass" },
      ),
    );
  });

  it("shows the checkpoint reason and sends optional retry feedback", async () => {
    harness.listRuns.mockResolvedValue([{ ...run, status: "success" }]);
    harness.diff.mockResolvedValue({
      patch: "diff --git a/products.html b/products.html",
    });
    render(
      <NodeDrawerRoute
        dependencies={[]}
        node={{ ...node, status: "blocked" }}
        onClose={vi.fn()}
        onDeleteNode={vi.fn()}
        onUpdateNode={vi.fn()}
        sessionId="sess_one"
      />,
      { wrapper },
    );

    expect(
      await screen.findByText(
        /branch contains changes, but the checkpoint was not recognized/,
      ),
    ).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Retry feedback"), {
      target: { value: "Keep the existing products page" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() =>
      expect(harness.retry).toHaveBeenCalledWith(
        "sess_one",
        "node_one",
        "Keep the existing products page",
      ),
    );
  });

  it("resumes an upstream-blocked node without requesting a missing diff", async () => {
    harness.listRuns.mockResolvedValue([]);
    render(
      <NodeDrawerRoute
        dependencies={["products_page"]}
        node={{
          ...node,
          status: "blocked",
          worktree_path: null,
          branch: null,
          base_ref: null,
        }}
        onClose={vi.fn()}
        onDeleteNode={vi.fn()}
        onUpdateNode={vi.fn()}
        sessionId="sess_one"
      />,
      { wrapper },
    );

    expect(
      await screen.findByText(/blocked dependency \(products_page\)/),
    ).toBeTruthy();
    expect(harness.diff).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Resume graph" }));
    await waitFor(() =>
      expect(harness.runGraph).toHaveBeenCalledWith("sess_one"),
    );
    expect(harness.retry).not.toHaveBeenCalled();
  });
});
