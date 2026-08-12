// @vitest-environment jsdom

import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AcceptanceResult, Node, Run } from "@/api/client";
import { NodeDrawer } from "@/components/graph/NodeDrawer";

function node(status: Node["status"]): Node {
  return {
    id: "node_one",
    session_id: "sess_one",
    name: "Implement API",
    prompt: "Build it",
    acceptance_criteria: ["Tests pass"],
    harness: "codex",
    model: "gpt-5.6-terra",
    touches: [],
    estimated_effort: null,
    worktree_path: "/workspace/node_one",
    branch: "agenthub/node_one",
    base_ref: "abc",
    status,
    created_ms: 1,
    updated_ms: 1,
  };
}

const run: Run = {
  id: "run_one",
  node_id: "node_one",
  session_id: "sess_one",
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

const acceptance: AcceptanceResult = {
  node_id: "node_one",
  attempt: 1,
  position: 0,
  criterion: "Tests pass",
  outcome: "unevaluated",
  created_ms: 1,
  updated_ms: 1,
};

function props(status: Node["status"]) {
  return {
    node: node(status),
    dependencies: [],
    runs: [run],
    events: [],
    summary: null,
    patch: "",
    acceptance: [],
    pendingAction: null,
    error: null,
    onClose: vi.fn(),
    onRun: vi.fn(),
    onKill: vi.fn(),
    onRetry: vi.fn(),
    onApprove: vi.fn(),
    onReject: vi.fn(),
  } as const;
}

describe("node drawer states", () => {
  afterEach(cleanup);

  it("shows a running node's live feed and kill action", () => {
    const callbacks = props("running");
    render(
      <NodeDrawer
        {...callbacks}
        events={[
          {
            type: "assistant_text",
            run_id: "run_one",
            ts: 2,
            text: "Streaming result",
          },
        ]}
      />,
    );

    expect(screen.getByText("Streaming result")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Kill" }));
    expect(callbacks.onKill).toHaveBeenCalledOnce();
  });

  it("keeps ready-node actions inside a scrollable details region", () => {
    const callbacks = props("ready");
    render(<NodeDrawer {...callbacks} />);

    const details = screen.getByRole("region", { name: "Node details" });
    expect(details.className).toContain("overflow-y-auto");
    expect(
      within(details).getByRole("button", { name: "Run ready nodes" }),
    ).toBeTruthy();
  });

  it("offers criterion outcomes plus approve and reject during review", () => {
    const callbacks = props("awaiting_review");
    render(
      <NodeDrawer
        {...callbacks}
        acceptance={[acceptance]}
        patch={"diff --git a/api.py b/api.py"}
      />,
    );

    fireEvent.change(screen.getByLabelText("Outcome for Tests pass"), {
      target: { value: "pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Approve merge" }));
    expect(callbacks.onApprove).toHaveBeenCalledWith({ 0: "pass" });

    fireEvent.change(screen.getByLabelText("Rejection feedback"), {
      target: { value: "Cover the failure path" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reject and retry" }));
    expect(callbacks.onReject).toHaveBeenCalledWith("Cover the failure path", {
      0: "pass",
    });
    expect(screen.getByText(/diff --git/)).toBeTruthy();
  });
});
