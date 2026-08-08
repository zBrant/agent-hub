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
import type { Dashboard, DashboardPeriod } from "@/api/client";
import { DashboardRoute } from "@/routes/DashboardRoute";
import { useSystemMetricsStore } from "@/stores/system-metrics-store";

const harness = vi.hoisted(() => ({ getDashboard: vi.fn() }));

vi.mock("@/api/client", () => ({
  api: { getDashboard: harness.getDashboard },
}));

const tokens = {
  input_tokens: 9_467,
  output_tokens: 709,
  cache_read_tokens: 62_208,
  cache_write_tokens: 0,
  total_tokens: 72_384,
} as const;

function snapshot(period: DashboardPeriod): Dashboard {
  const usage = {
    key: "codex",
    tokens,
    estimated_equivalent_cost_usd: 0.0498545,
    cost_complete: false,
  } as const;
  return {
    period,
    since_ms: 1,
    generated_ms: 2,
    usage: { ...usage, key: "total" },
    by_harness: [usage],
    by_model: [{ ...usage, key: "gpt-5.6-terra" }],
    active_session_count: 1,
    running_node_count: 2,
    blocked_node_count: 1,
    node_completion_rate: 0.5,
    event_feed: [],
    active_sessions: [
      {
        id: "sess_phase_two",
        title: "Accepted graph",
        status: "paused",
        created_ms: 1,
        elapsed_ms: 3_900_000,
        total_nodes: 2,
        completed_nodes: 1,
        blocked_nodes: 1,
        harnesses: ["codex"],
        usage: { ...usage, key: "sess_phase_two" },
      },
    ],
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/dashboard"]}>
        <Routes>
          <Route path="/dashboard" element={children} />
          <Route path="/sessions/:id" element={<p>Selected session</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

describe("dashboard route", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useSystemMetricsStore.getState().reset();
    harness.getDashboard.mockImplementation((period: DashboardPeriod) =>
      Promise.resolve(snapshot(period)),
    );
  });

  afterEach(cleanup);

  it("renders four-field usage, active progress, and partial cost honestly", async () => {
    render(<DashboardRoute />, { wrapper });

    expect((await screen.findAllByText("72.4K")).length).toBeGreaterThanOrEqual(
      3,
    );
    expect(screen.getByText("$0.0499")).toBeTruthy();
    expect(screen.getByText("Partial — unpriced usage exists")).toBeTruthy();
    expect(screen.getByText("1/2 nodes · 1h 5m")).toBeTruthy();
    expect(screen.getAllByTitle("Cache read: 62,208")).toHaveLength(2);

    fireEvent.click(screen.getByText("Accepted graph"));
    expect(await screen.findByText("Selected session")).toBeTruthy();
  });

  it("refetches when the operator changes period", async () => {
    render(<DashboardRoute />, { wrapper });
    await screen.findByText("Dashboard");
    fireEvent.click(screen.getByRole("button", { name: "7 days" }));

    await waitFor(() =>
      expect(harness.getDashboard).toHaveBeenCalledWith("7d"),
    );
  });

  it("renders live system gauges and the active process tree", async () => {
    useSystemMetricsStore.getState().push({
      ts: 20,
      cpu_percent: 34.5,
      cpu_per_core: [20, 49],
      memory_total_bytes: 1_073_741_824,
      memory_used_bytes: 536_870_912,
      memory_available_bytes: 536_870_912,
      memory_percent: 50,
      swap_total_bytes: 0,
      swap_used_bytes: 0,
      swap_free_bytes: 0,
      swap_percent: 0,
      disk_total_bytes: 2_147_483_648,
      disk_used_bytes: 1_073_741_824,
      disk_free_bytes: 1_073_741_824,
      disk_percent: 50,
      processes: [
        {
          node_id: "node_live",
          pid: 123,
          harness: "codex",
          rss_bytes: 268_435_456,
          cpu_percent: 12.5,
          uptime_ms: 3_900_000,
          process_count: 3,
        },
      ],
    });

    render(<DashboardRoute />, { wrapper });

    expect(await screen.findByText("System health")).toBeTruthy();
    expect(screen.getByText("34.5%")).toBeTruthy();
    expect(screen.getByText("node_live")).toBeTruthy();
    expect(screen.getByText("256.0 MiB")).toBeTruthy();
    expect(screen.getByText("1h 5m")).toBeTruthy();
  });

  it("deep-links a meaningful transition to its graph node", async () => {
    const data = snapshot("today");
    harness.getDashboard.mockResolvedValue({
      ...data,
      event_feed: [
        {
          id: 7,
          session_id: "sess_phase_two",
          session_title: "Accepted graph",
          node_id: "node_failed",
          node_name: "Run verification",
          status: "failed",
          ts: 20,
        },
      ],
    });
    render(<DashboardRoute />, { wrapper });

    const link = await screen.findByRole("link", { name: /Run verification/ });
    expect(link.getAttribute("href")).toBe(
      "/sessions/sess_phase_two?node=node_failed",
    );
    expect(screen.getByText("Failed")).toBeTruthy();
  });

  it("renders empty usage without inventing cost", async () => {
    const empty = snapshot("today");
    harness.getDashboard.mockResolvedValue({
      ...empty,
      usage: {
        ...empty.usage,
        tokens: {
          input_tokens: 0,
          output_tokens: 0,
          cache_read_tokens: 0,
          cache_write_tokens: 0,
          total_tokens: 0,
        },
        estimated_equivalent_cost_usd: null,
        cost_complete: true,
      },
      by_harness: [],
      by_model: [],
      active_sessions: [],
      active_session_count: 0,
      running_node_count: 0,
      blocked_node_count: 0,
      node_completion_rate: null,
    });
    render(<DashboardRoute />, { wrapper });

    expect(await screen.findByText("No active sessions")).toBeTruthy();
    expect(screen.getAllByText("No usage in this period.")).toHaveLength(2);
    expect(screen.getAllByText("—")).toHaveLength(2);
  });
});
