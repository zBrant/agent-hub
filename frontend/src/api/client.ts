import type { AgentEvent } from "@/api/events";
import type { components } from "@/api/schema";

export type Session = components["schemas"]["SessionResponse"];
export type Node = components["schemas"]["NodeResponse"];
export type Run = components["schemas"]["RunResponse"];
export type RunSummary = components["schemas"]["RunSummaryResponse"];
export type Diff = components["schemas"]["DiffResponse"];

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { Accept: "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try {
      const payload: unknown = await response.json();
      if (
        typeof payload === "object" &&
        payload !== null &&
        "detail" in payload &&
        typeof payload.detail === "string"
      ) {
        message = payload.detail;
      }
    } catch {
      // The status remains actionable when the error body is not JSON.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

function sessionPath(sessionId: string): string {
  return `/api/sessions/${encodeURIComponent(sessionId)}`;
}

function runPath(sessionId: string, runId: string): string {
  return `${sessionPath(sessionId)}/runs/${encodeURIComponent(runId)}`;
}

export const api = {
  listSessions: () => request<readonly Session[]>("/api/sessions"),
  getSession: (sessionId: string) => request<Session>(sessionPath(sessionId)),
  getNode: (sessionId: string) =>
    request<Node>(`${sessionPath(sessionId)}/node`),
  listRuns: (sessionId: string) =>
    request<readonly Run[]>(`${sessionPath(sessionId)}/runs`),
  getRunSummary: (sessionId: string, runId: string) =>
    request<RunSummary>(`${runPath(sessionId, runId)}/summary`),
  getRunEvents: (sessionId: string, runId: string) =>
    request<readonly AgentEvent[]>(`${runPath(sessionId, runId)}/events`),
  getDiff: (sessionId: string) =>
    request<Diff>(`${sessionPath(sessionId)}/diff`),
  start: (sessionId: string) =>
    request<unknown>(`${sessionPath(sessionId)}/runs`, { method: "POST" }),
  kill: (sessionId: string) =>
    request<unknown>(`${sessionPath(sessionId)}/kill`, { method: "POST" }),
  retry: (sessionId: string) =>
    request<unknown>(`${sessionPath(sessionId)}/retry`, { method: "POST" }),
  approve: (sessionId: string) =>
    request<unknown>(`${sessionPath(sessionId)}/approve`, { method: "POST" }),
};
