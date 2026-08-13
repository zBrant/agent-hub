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
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Graph } from "@/api/client";
import { SessionRoute } from "@/routes/SessionRoute";

const harness = vi.hoisted(() => ({
  graph: null as Graph | null,
  getGraph: vi.fn(),
  getSession: vi.fn(),
  getNode: vi.fn(),
  listRuns: vi.fn(),
  getDiff: vi.fn(),
  updateNode: vi.fn(),
  approveGraph: vi.fn(),
}));

vi.mock("@/components/graph/GraphWorkspace", () => ({
  GraphWorkspace: (props: { graph: Graph; onApprove: () => Promise<void> }) => (
    <div>
      <span>Graph workspace: {props.graph.nodes.length}</span>
      <button onClick={() => void props.onApprove()} type="button">
        Confirm proposal
      </button>
    </div>
  ),
}));

vi.mock("@/api/client", () => ({
  ApiError: class ApiError extends Error {
    status = 500;
  },
  api: {
    getGraph: harness.getGraph,
    getSession: harness.getSession,
    getNode: harness.getNode,
    listRuns: harness.listRuns,
    getDiff: harness.getDiff,
    updateNode: harness.updateNode,
    deleteNode: vi.fn(),
    addDependency: vi.fn(),
    removeDependency: vi.fn(),
    approveGraph: harness.approveGraph,
  },
}));

vi.mock("@/ws/WebSocketProvider", () => ({
  useWebSocketClient: () => null,
}));

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

describe("session graph route", () => {
  afterEach(cleanup);

  it("renders a multi-node proposal and sends approval through the graph API", async () => {
    const session = {
      id: "sess_one",
      title: "Build graph",
      repo_path: "/repo",
      workspace_root: "/workspace",
      integration_branch: "agenthub/sess_one/integration",
      final_branch: "feature/graph-result",
      auto_merge: false,
      status: "planning",
      created_ms: 1,
      updated_ms: 1,
    } as const;
    const makeNode = (id: string) => ({
      id,
      session_id: session.id,
      name: id,
      prompt: id,
      acceptance_criteria: [],
      requires_review: true,
      harness: "codex",
      model: null,
      touches: [],
      estimated_effort: null,
      worktree_path: null,
      branch: null,
      base_ref: null,
      status: "pending" as const,
      created_ms: 1,
      updated_ms: 1,
    });
    const graph: Graph = {
      session,
      nodes: [makeNode("node_a"), makeNode("node_b")],
      edges: [],
    };
    harness.getGraph.mockResolvedValue(graph);
    harness.getSession.mockResolvedValue(session);
    harness.getNode.mockResolvedValue(graph.nodes[0]);
    harness.listRuns.mockResolvedValue([]);
    harness.getDiff.mockResolvedValue({ patch: "" });
    harness.approveGraph.mockResolvedValue({
      ...graph,
      nodes: graph.nodes.map((node) => ({ ...node, status: "ready" })),
    });

    render(<SessionRoute />, { wrapper });
    expect(await screen.findByText("Graph workspace: 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm proposal" }));

    await waitFor(() =>
      expect(harness.approveGraph).toHaveBeenCalledWith("sess_one"),
    );
  });
});
