// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AISettings, UpdateAISettings } from "@/api/client";
import { SettingsRoute } from "@/routes/SettingsRoute";

const harness = vi.hoisted(() => ({
  getAISettings: vi.fn(),
  updateAISettings: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    getAISettings: harness.getAISettings,
    updateAISettings: harness.updateAISettings,
  },
}));

const settings: AISettings = {
  planner: { backend: "harness", harness: "claude-code", model: null },
  search: { backend: "harness", harness: "codex", model: "gpt-5.6-sol" },
  planner_effort: "high",
  planner_options: [
    {
      backend: "api",
      harness: null,
      models: ["claude-opus-5", "claude-sonnet-5"],
      is_spend: true,
      supports_effort: true,
    },
    {
      backend: "harness",
      harness: "claude-code",
      models: ["claude-opus-5", "claude-sonnet-5"],
      is_spend: false,
      supports_effort: false,
    },
  ],
  search_options: [
    {
      backend: "api",
      harness: null,
      models: ["claude-sonnet-5"],
      is_spend: true,
      supports_effort: false,
    },
    {
      backend: "harness",
      harness: "codex",
      models: ["gpt-5.6-sol", "gpt-5.6-terra"],
      is_spend: false,
      supports_effort: false,
    },
  ],
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe("AI settings", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    harness.getAISettings.mockResolvedValue(settings);
    harness.updateAISettings.mockImplementation((update: UpdateAISettings) =>
      Promise.resolve({ ...settings, ...update }),
    );
  });

  afterEach(cleanup);

  it("saves planner, search, and effort as one settings update", async () => {
    render(<SettingsRoute />, { wrapper });

    const planner = await screen.findByRole("group", {
      name: "Planner runtime",
    });
    const search = screen.getByRole("group", {
      name: "Code Search runtime",
    });
    expect(
      within(planner).getByRole("radio", { name: /claude-code/ }),
    ).toHaveProperty("checked", true);
    expect(within(search).getByRole("radio", { name: /codex/ })).toHaveProperty(
      "checked",
      true,
    );

    fireEvent.click(
      within(planner).getByRole("radio", { name: /Anthropic API/ }),
    );
    const plannerModel = screen.getAllByLabelText("Model")[0];
    if (!plannerModel) throw new Error("Planner model select is missing");
    fireEvent.change(plannerModel, {
      target: { value: "claude-opus-5" },
    });
    fireEvent.change(screen.getByLabelText("Planner effort"), {
      target: { value: "xhigh" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save settings" }));

    await waitFor(() => expect(harness.updateAISettings).toHaveBeenCalled());
    expect(harness.updateAISettings.mock.calls[0]?.[0]).toEqual({
      planner: {
        backend: "api",
        harness: null,
        model: "claude-opus-5",
      },
      search: {
        backend: "harness",
        harness: "codex",
        model: "gpt-5.6-sol",
      },
      planner_effort: "xhigh",
    });
    expect((await screen.findByRole("status")).textContent).toContain(
      "Settings saved.",
    );
  });

  it("explains API billing without presenting an API key field", async () => {
    render(<SettingsRoute />, { wrapper });
    const search = await screen.findByRole("group", {
      name: "Code Search runtime",
    });

    fireEvent.click(
      within(search).getByRole("radio", { name: /Anthropic API/ }),
    );

    expect(
      screen.getByText(/credentials configured on the AgentHub server/),
    ).toBeTruthy();
    expect(screen.getByText(/real provider charges/)).toBeTruthy();
    expect(screen.queryByLabelText(/API key/i)).toBeNull();
  });
});
