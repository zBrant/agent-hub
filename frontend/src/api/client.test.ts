import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";

const fetchMock = vi.fn();

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

describe("API client", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => vi.unstubAllGlobals());

  it("loads AI runtime settings", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await api.getAISettings();

    expect(fetchMock).toHaveBeenCalledWith("/api/settings/ai", {
      headers: { Accept: "application/json" },
    });
  });

  it("updates only mutable AI runtime settings", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));
    const update = {
      planner: { backend: "harness" as const, harness: "codex", model: null },
      search: {
        backend: "api" as const,
        harness: null,
        model: "claude-sonnet-5",
      },
      planner_effort: "high" as const,
    };

    await api.updateAISettings(update);

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/settings/ai");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual(update);
  });

  it("discovers projects without a session target", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ projects: [] }));

    await api.listSearchProjects();

    expect(fetchMock).toHaveBeenCalledWith("/api/search/projects", {
      headers: { Accept: "application/json" },
    });
  });

  it("answers against an explicit project and branch", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await api.answerSearchQuestion("pricing", "feature/rules", "Why?");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/search/answer");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      project_id: "pricing",
      branch: "feature/rules",
      question: "Why?",
    });
  });

  it("reads a file against an explicit project and branch", async () => {
    fetchMock.mockResolvedValue(jsonResponse({}));

    await api.readSearchFile("pricing", "feature/rules", "src/rules.py", {
      startLine: 2,
      endLine: 4,
    });

    const [path] = fetchMock.mock.calls[0] as [string, RequestInit];
    const url = new URL(path, "http://agenthub.local");
    expect(url.pathname).toBe("/api/search/file");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      project_id: "pricing",
      branch: "feature/rules",
      path: "src/rules.py",
      start_line: "2",
      end_line: "4",
    });
    expect(url.searchParams.has("session_id")).toBe(false);
  });
});
