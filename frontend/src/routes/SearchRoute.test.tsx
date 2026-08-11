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
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  AgentSearch,
  FileRead,
  SearchBranch,
  SearchProject,
} from "@/api/client";
import { SearchRoute } from "@/routes/SearchRoute";

const harness = vi.hoisted(() => ({
  listSearchProjects: vi.fn(),
  answerSearchQuestion: vi.fn(),
  readSearchFile: vi.fn(),
}));

vi.mock("@/api/client", () => ({
  api: {
    listSearchProjects: harness.listSearchProjects,
    answerSearchQuestion: harness.answerSearchQuestion,
    readSearchFile: harness.readSearchFile,
  },
}));

const project: SearchProject = {
  id: "pricing",
  name: "pricing",
  repo_path: "/repo/pricing",
  branches: [branch("feature/pricing", false), branch("main", true)],
};

function branch(name: string, isHead = false): SearchBranch {
  return {
    name,
    commit: "a".repeat(40),
    is_head: isHead,
  };
}

function answer(citations = true): AgentSearch {
  return {
    complete: true,
    limit_reason: null,
    message: "answer validated against read-file evidence",
    turns: 3,
    tool_calls: 4,
    bytes_read: 512,
    evidence: [{ path: "rules.py", line: 2, end_line: 3 }],
    claims: [
      {
        text: "Recurring customers receive ten percent off.",
        citations: citations
          ? [
              {
                path: "rules.py",
                line: 2,
                end_line: 3,
                content_hash: "original-hash",
              },
            ]
          : [],
      },
    ],
    usage: {
      model: "claude-sonnet-5",
      input_tokens: 20,
      output_tokens: 10,
      cache_read_tokens: 30,
      cache_write_tokens: 0,
      total_tokens: 60,
      cost_usd: 0.001,
      price_table_version: 1,
      requests: 3,
    },
  };
}

const file: FileRead = {
  path: "rules.py",
  lines: [
    { line: 2, text: "if customer.is_recurring():" },
    { line: 3, text: "    return 0.10" },
  ],
  truncated: false,
  content_hash: "original-hash",
};

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

async function askQuestion() {
  fireEvent.change(screen.getByLabelText("Repository question"), {
    target: { value: "What is the recurring discount?" },
  });
  fireEvent.click(screen.getByRole("button", { name: "Ask" }));
  await screen.findByText("Recurring customers receive ten percent off.");
}

describe("code search route", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    const stored = new Map<string, string>();
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
      },
    });
    Object.defineProperty(window, "ResizeObserver", {
      configurable: true,
      value: class ResizeObserver {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    });
    harness.listSearchProjects.mockResolvedValue({ projects: [project] });
    harness.answerSearchQuestion.mockResolvedValue(answer());
    harness.readSearchFile.mockResolvedValue(file);
  });

  afterEach(cleanup);

  it("opens every claim citation at its exact file and line range", async () => {
    render(<SearchRoute />, { wrapper });
    await screen.findByRole("option", {
      name: "main — HEAD",
    });
    await askQuestion();

    fireEvent.click(
      screen.getByRole("button", { name: "Open rules.py lines 2-3" }),
    );
    const source = await screen.findByLabelText("rules.py lines 2 through 3");
    expect(source.textContent).toContain("return 0.10");

    expect(harness.readSearchFile).toHaveBeenCalledWith(
      "pricing",
      "main",
      "rules.py",
      { startLine: 2, endLine: 3 },
    );
    expect(source).toBeTruthy();
  });

  it("marks the citation stale when the exact cited lines changed", async () => {
    harness.readSearchFile.mockResolvedValue({
      ...file,
      lines: [{ line: 2, text: "if customer.is_enterprise():" }],
      content_hash: "changed-hash",
    });
    render(<SearchRoute />, { wrapper });
    await screen.findByRole("option", {
      name: "main — HEAD",
    });
    await askQuestion();
    fireEvent.click(
      screen.getByRole("button", { name: "Open rules.py lines 2-3" }),
    );

    expect((await screen.findByRole("alert")).textContent).toContain(
      "This citation is stale",
    );
  });

  it("refuses to render a server claim without a linked citation", async () => {
    harness.answerSearchQuestion.mockResolvedValue(answer(false));
    render(<SearchRoute />, { wrapper });
    await screen.findByRole("option", {
      name: "main — HEAD",
    });

    fireEvent.change(screen.getByLabelText("Repository question"), {
      target: { value: "Make an unsupported claim" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask" }));

    await waitFor(() =>
      expect(
        screen.getByText(
          "The response contained no linked claims and was not rendered.",
        ),
      ).toBeTruthy(),
    );
    expect(
      screen.queryByText("Recurring customers receive ten percent off."),
    ).toBeNull();
  });

  it("discovers projects and searches the selected branch", async () => {
    const pricingProject: SearchProject = {
      ...project,
      branches: [
        branch("feature/pricing"),
        branch("experiment"),
        branch("main", true),
      ],
    };
    const billingProject: SearchProject = {
      id: "billing",
      name: "billing",
      repo_path: "/repo/billing",
      branches: [branch("release"), branch("main", true)],
    };
    harness.listSearchProjects.mockResolvedValue({
      projects: [pricingProject, billingProject],
    });

    render(<SearchRoute />, { wrapper });

    const projectSelect = await screen.findByLabelText("Project");
    const branchSelect = screen.getByLabelText("Branch");
    await waitFor(() =>
      expect(projectSelect.querySelectorAll("option")).toHaveLength(2),
    );
    await waitFor(() =>
      expect(branchSelect.querySelectorAll("option")).toHaveLength(3),
    );
    fireEvent.change(projectSelect, { target: { value: "billing" } });
    await waitFor(() =>
      expect((branchSelect as HTMLSelectElement).value).toBe("main"),
    );
    expect(branchSelect.querySelectorAll("option")).toHaveLength(2);

    fireEvent.change(projectSelect, { target: { value: "pricing" } });
    await waitFor(() =>
      expect((branchSelect as HTMLSelectElement).value).toBe("main"),
    );
    fireEvent.change(branchSelect, {
      target: { value: "experiment" },
    });
    await waitFor(() =>
      expect((branchSelect as HTMLSelectElement).value).toBe("experiment"),
    );
    await askQuestion();

    expect(harness.answerSearchQuestion).toHaveBeenCalledWith(
      "pricing",
      "experiment",
      "What is the recurring discount?",
    );
  });
});
