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
import { SessionsIndexRoute } from "@/routes/SessionsIndexRoute";

const harness = vi.hoisted(() => ({
  listSessions: vi.fn(),
  planGraph: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    listSessions: harness.listSessions,
    planGraph: harness.planGraph,
  },
}));

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/sessions"]}>
        <Routes>
          <Route path="/sessions" element={children} />
          <Route path="/sessions/:id" element={<p>Graph proposal</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("sessions planner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    harness.listSessions.mockResolvedValue([]);
  });

  afterEach(cleanup);

  it("creates a gated proposal from a repository and objective", async () => {
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });

    fireEvent.change(screen.getByLabelText("Repository path"), {
      target: { value: "  /repo/project  " },
    });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: "  Build the graph  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));

    await waitFor(() => expect(harness.planGraph).toHaveBeenCalled());
    expect(harness.planGraph.mock.calls[0]?.[0]).toEqual({
      repo_path: "/repo/project",
      objective: "Build the graph",
      auto_merge: false,
      base_ref: "HEAD",
      context: null,
    });
    expect(await screen.findByText("Graph proposal")).toBeTruthy();
  });

  it("renders a safe planner failure message", async () => {
    harness.planGraph.mockRejectedValue(new Error("planner API unavailable"));
    render(<SessionsIndexRoute />, { wrapper });

    fireEvent.change(screen.getByLabelText("Repository path"), {
      target: { value: "/repo/project" },
    });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: "Build the graph" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));

    expect((await screen.findByRole("alert")).textContent).toBe(
      "planner API unavailable",
    );
  });
});
