// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentEvent } from "@/api/events";
import { SessionRoute } from "@/routes/SessionRoute";
import { useSessionFeedStore } from "@/stores/session-feed-store";

const harness = vi.hoisted(() => ({
  handler: null as ((event: AgentEvent) => void) | null,
  session: vi.fn(),
  graph: vi.fn(),
  node: vi.fn(),
  runs: vi.fn(),
  summary: vi.fn(),
  events: vi.fn(),
  diff: vi.fn(),
  start: vi.fn(),
  kill: vi.fn(),
  retry: vi.fn(),
  approve: vi.fn(),
  graphAction: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  ApiError: class ApiError extends Error {
    status = 500;
  },
  api: {
    getSession: harness.session,
    getGraph: harness.graph,
    updateNode: harness.graphAction,
    deleteNode: harness.graphAction,
    addDependency: harness.graphAction,
    removeDependency: harness.graphAction,
    approveGraph: harness.graphAction,
    getNode: harness.node,
    listRuns: harness.runs,
    getRunSummary: harness.summary,
    getRunEvents: harness.events,
    getDiff: harness.diff,
    start: harness.start,
    kill: harness.kill,
    retry: harness.retry,
    approve: harness.approve,
  },
}));

vi.mock("@/ws/WebSocketProvider", () => ({
  useWebSocketClient: () => ({
    subscribe: (topic: string, handler: (event: AgentEvent) => void) => {
      if (topic.startsWith("session:")) harness.handler = handler;
      return () => {
        if (topic.startsWith("session:")) harness.handler = null;
      };
    },
  }),
}));

const session = {
  id: "sess_one",
  title: "Build the feature",
  repo_path: "/repo",
  workspace_root: "/workspace",
  integration_branch: "agenthub/sess_one/integration",
  auto_merge: true,
  status: "planning",
  created_ms: 1,
  updated_ms: 1,
} as const;

function node(status: "ready" | "running" | "done" | "failed" | "blocked") {
  return {
    id: "node_one",
    session_id: session.id,
    name: "main",
    prompt: "Build it",
    acceptance_criteria: [],
    harness: "codex",
    model: "gpt-5.6-terra",
    worktree_path: "/workspace/node_one",
    branch: "agenthub/node_one",
    base_ref: "abc",
    status,
    created_ms: 1,
    updated_ms: 1,
  } as const;
}

function run(status: "running" | "success" | "interrupted") {
  return {
    id: "run_one",
    node_id: "node_one",
    session_id: session.id,
    attempt: 1,
    status,
    harness: "codex",
    model: "gpt-5.6-terra",
    pid: 42,
    harness_session_id: "thread",
    harness_version: "1",
    started_ms: 10,
    finished_ms: status === "running" ? null : 20,
    exit_code: status === "success" ? 0 : null,
    summary: null,
    event_count: status === "running" ? 1 : 2,
    permission_denial_count: 0,
    created_ms: 1,
  } as const;
}

function summary(trusted: boolean) {
  return {
    run_id: "run_one",
    trusted,
    tokens: {
      input_tokens: 1,
      output_tokens: 2,
      cache_read_tokens: 3,
      cache_write_tokens: 4,
      total_tokens: 10,
    },
    estimated_equivalent_cost_usd: 0.001,
    cost_complete: true,
  } as const;
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/sessions/sess_one"]}>
        <Routes>
          <Route path="/sessions/:id" element={children} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("live session route", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSessionFeedStore.setState({ eventsByRun: {} });
    harness.session.mockResolvedValue(session);
    harness.graph.mockResolvedValue({
      session,
      nodes: [node("ready")],
      edges: [],
    });
    harness.diff.mockResolvedValue({ patch: "" });
    harness.events.mockResolvedValue([]);
    harness.summary.mockResolvedValue(summary(true));
  });

  afterEach(cleanup);

  it("moves ready → running → done from a fake streamed run", async () => {
    let status: "ready" | "running" | "done" = "ready";
    let runs: readonly ReturnType<typeof run>[] = [];
    let finishStart: (() => void) | undefined;
    harness.node.mockImplementation(() => Promise.resolve(node(status)));
    harness.runs.mockImplementation(() => Promise.resolve(runs));
    harness.start.mockImplementation(
      () =>
        new Promise((resolve) => {
          finishStart = () => resolve({});
        }),
    );

    render(<SessionRoute />, { wrapper });
    fireEvent.click(await screen.findByRole("button", { name: "Start run" }));

    status = "running";
    runs = [run("running")];
    harness.handler?.({
      type: "run_started",
      run_id: "run_one",
      ts: 10,
      harness: "codex",
      model: "gpt-5.6-terra",
      cwd: "/workspace/node_one",
      pid: 42,
    });
    expect(
      await screen.findByRole("button", { name: "Kill run" }),
    ).toBeTruthy();

    status = "done";
    runs = [run("success")];
    harness.handler?.({
      type: "run_finished",
      run_id: "run_one",
      ts: 20,
      status: "success",
      exit_code: 0,
    });
    finishStart?.();

    await waitFor(() => expect(screen.getByText("Done")).toBeTruthy());
    expect(screen.getByText(/Run finished/)).toBeTruthy();
    expect(screen.getByText("Estimated equivalent cost")).toBeTruthy();
  });

  it("restores an interrupted failed run and offers retry", async () => {
    harness.node.mockResolvedValue(node("failed"));
    harness.runs.mockResolvedValue([run("interrupted")]);
    harness.events.mockResolvedValue([
      {
        type: "run_finished",
        run_id: "run_one",
        ts: 20,
        status: "interrupted",
        exit_code: null,
      },
    ]);

    render(<SessionRoute />, { wrapper });

    expect(await screen.findByRole("button", { name: "Retry" })).toBeTruthy();
    expect(await screen.findByText("interrupted")).toBeTruthy();
  });

  it("surfaces parser drift as unsafe on a terminal run", async () => {
    harness.node.mockResolvedValue(node("blocked"));
    harness.runs.mockResolvedValue([run("success")]);
    harness.summary.mockResolvedValue(summary(false));

    render(<SessionRoute />, { wrapper });

    expect(await screen.findByText("Parser drift — unsafe")).toBeTruthy();
  });
});
