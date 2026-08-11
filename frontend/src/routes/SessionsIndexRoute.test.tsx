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
import type { PlannerOptions } from "@/api/client";
import { SessionsIndexRoute } from "@/routes/SessionsIndexRoute";

const harness = vi.hoisted(() => ({
  listSessions: vi.fn(),
  planGraph: vi.fn(),
  getPlannerOptions: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    listSessions: harness.listSessions,
    planGraph: harness.planGraph,
    getPlannerOptions: harness.getPlannerOptions,
  },
}));

const CLAUDE_MODELS = ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"];
const CODEX_MODELS = ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"];

/** The live server's payload, with the default it ships with. */
function plannerOptions(
  fallback: Partial<PlannerOptions["default"]> = {},
): PlannerOptions {
  return {
    default: {
      backend: "harness",
      harness: "claude-code",
      model: null,
      selectable: true,
      ...fallback,
    },
    options: [
      {
        backend: "api",
        harness: null,
        models: CLAUDE_MODELS,
        is_spend: true,
        supports_effort: true,
      },
      {
        backend: "harness",
        harness: "claude-code",
        models: CLAUDE_MODELS,
        is_spend: false,
        supports_effort: false,
      },
      {
        backend: "harness",
        harness: "codex",
        models: CODEX_MODELS,
        is_spend: false,
        supports_effort: false,
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
      <MemoryRouter initialEntries={["/sessions"]}>
        <Routes>
          <Route path="/sessions" element={children} />
          <Route path="/sessions/:id" element={<p>Graph proposal</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

function fillObjective() {
  fireEvent.change(screen.getByLabelText("Repository path"), {
    target: { value: "/repo/project" },
  });
  fireEvent.change(screen.getByLabelText("Objective"), {
    target: { value: "Build the graph" },
  });
}

function submit() {
  fireEvent.click(screen.getByRole("button", { name: "Create proposal" }));
}

function submitButton(): HTMLButtonElement {
  const button = screen.getByRole("button", { name: "Create proposal" });
  if (!(button instanceof HTMLButtonElement)) throw new Error("no submit");
  return button;
}

function modelSelect(): HTMLSelectElement {
  const select = screen.getByLabelText("Planner model");
  if (!(select instanceof HTMLSelectElement)) throw new Error("no model");
  return select;
}

function radio(name: RegExp): HTMLInputElement {
  const input = screen.getByRole("radio", { name });
  if (!(input instanceof HTMLInputElement)) throw new Error("no radio");
  return input;
}

/** The single `planner` value posted, or the absence of the key. */
function postedPlanner(): unknown {
  const body: unknown = harness.planGraph.mock.calls[0]?.[0];
  if (typeof body !== "object" || body === null) throw new Error("no body");
  return Object.hasOwn(body, "planner")
    ? (body as { planner: unknown }).planner
    : "<absent>";
}

describe("sessions planner", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    harness.listSessions.mockResolvedValue([]);
    harness.getPlannerOptions.mockResolvedValue(plannerOptions());
  });

  afterEach(cleanup);

  it("creates a gated proposal from a repository and objective", async () => {
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });
    await screen.findByRole("radio", { name: /claude-code/ });

    fireEvent.change(screen.getByLabelText("Repository path"), {
      target: { value: "  /repo/project  " },
    });
    fireEvent.change(screen.getByLabelText("Objective"), {
      target: { value: "  Build the graph  " },
    });
    submit();

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

  it("preselects the server default and sends no planner for it", async () => {
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });

    await screen.findByRole("radio", { name: /claude-code/ });
    expect(radio(/claude-code/).checked).toBe(true);
    expect(radio(/codex/).checked).toBe(false);
    expect(radio(/Anthropic API/).checked).toBe(false);
    // `model: null` is the default and a real choice, not an empty state.
    expect(modelSelect().value).toBe("");
    expect(modelSelect().selectedOptions[0]?.textContent).toContain(
      "whatever the CLI is configured for",
    );

    fillObjective();
    submit();

    await waitFor(() => expect(harness.planGraph).toHaveBeenCalled());
    expect(postedPlanner()).toBe("<absent>");
  });

  it("reconciles the model when the planner backend changes", async () => {
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });
    await screen.findByRole("radio", { name: /codex/ });

    fireEvent.click(radio(/codex/));
    expect(screen.queryByRole("option", { name: "claude-opus-5" })).toBeNull();
    fireEvent.change(modelSelect(), { target: { value: "gpt-5.6-terra" } });
    expect(modelSelect().value).toBe("gpt-5.6-terra");

    // `gpt-5.6-terra` exists only under `codex`: it must not survive the move.
    fireEvent.click(radio(/Anthropic API/));
    expect(screen.queryByRole("option", { name: "gpt-5.6-terra" })).toBeNull();
    expect(modelSelect().value).toBe("claude-opus-5");

    fillObjective();
    submit();

    await waitFor(() => expect(harness.planGraph).toHaveBeenCalled());
    expect(postedPlanner()).toEqual({
      backend: "api",
      harness: null,
      model: "claude-opus-5",
    });
  });

  it("falls back to the CLI default when a harness lacks the model", async () => {
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });
    await screen.findByRole("radio", { name: /Anthropic API/ });

    fireEvent.click(radio(/Anthropic API/));
    fireEvent.change(modelSelect(), { target: { value: "claude-haiku-4-5" } });
    fireEvent.click(radio(/codex/));
    expect(modelSelect().value).toBe("");

    fillObjective();
    submit();

    await waitFor(() => expect(harness.planGraph).toHaveBeenCalled());
    expect(postedPlanner()).toEqual({
      backend: "harness",
      harness: "codex",
      model: null,
    });
  });

  it("separates API spend from subscription equivalence before submitting", async () => {
    render(<SessionsIndexRoute />, { wrapper });
    await screen.findByRole("radio", { name: /Anthropic API/ });

    // Both meanings are on the options themselves, before anything is picked.
    expect(radio(/Anthropic API/).labels?.[0]?.textContent).toContain(
      "Billed per token",
    );
    expect(radio(/claude-code/).labels?.[0]?.textContent).toContain(
      "Subscription",
    );

    // The preselected harness never calls its cost spend.
    expect(
      screen.getByText(/estimated equivalent, not new spend/),
    ).toBeTruthy();
    expect(screen.queryByText(/real spend/)).toBeNull();
    expect(screen.getByText(/decides its own planning depth/)).toBeTruthy();

    fireEvent.click(radio(/Anthropic API/));

    expect(screen.getByText(/real spend/).textContent).toContain(
      "Billed per token against your Anthropic API key",
    );
    expect(
      screen.queryByText(/estimated equivalent, not new spend/),
    ).toBeNull();
    // `supports_effort` is true here, so no disclaimer is owed.
    expect(screen.queryByText(/decides its own planning depth/)).toBeNull();
  });

  it("never preselects a default the server cannot use", async () => {
    harness.getPlannerOptions.mockResolvedValue(
      plannerOptions({ harness: "nope", selectable: false }),
    );
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });

    expect((await screen.findByRole("alert")).textContent).toContain(
      "cannot back a plan",
    );
    expect(screen.getByRole("alert").textContent).toContain("nope");
    expect(radio(/claude-code/).checked).toBe(false);
    expect(radio(/codex/).checked).toBe(false);
    expect(radio(/Anthropic API/).checked).toBe(false);
    expect(screen.queryByLabelText("Planner model")).toBeNull();

    fillObjective();
    expect(submitButton().disabled).toBe(true);
    submit();
    expect(harness.planGraph).not.toHaveBeenCalled();

    fireEvent.click(radio(/codex/));
    expect(submitButton().disabled).toBe(false);
    submit();

    await waitFor(() => expect(harness.planGraph).toHaveBeenCalled());
    expect(postedPlanner()).toEqual({
      backend: "harness",
      harness: "codex",
      model: null,
    });
  });

  it("still plans on the server default when the options query fails", async () => {
    harness.getPlannerOptions.mockRejectedValue(new Error("options offline"));
    harness.planGraph.mockResolvedValue({ session: { id: "sess_plan" } });
    render(<SessionsIndexRoute />, { wrapper });

    expect((await screen.findByRole("status")).textContent).toContain(
      "options offline",
    );
    expect(submitButton().disabled).toBe(false);

    fillObjective();
    submit();

    await waitFor(() => expect(harness.planGraph).toHaveBeenCalled());
    expect(postedPlanner()).toBe("<absent>");
    expect(await screen.findByText("Graph proposal")).toBeTruthy();
  });

  it("renders a safe planner failure message", async () => {
    harness.planGraph.mockRejectedValue(new Error("planner API unavailable"));
    render(<SessionsIndexRoute />, { wrapper });
    await screen.findByRole("radio", { name: /claude-code/ });

    fillObjective();
    submit();

    expect((await screen.findByRole("alert")).textContent).toBe(
      "planner API unavailable",
    );
  });
});
