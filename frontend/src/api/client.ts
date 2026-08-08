import type { AgentEvent } from "@/api/events";
import type { components } from "@/api/schema";

export type Session = components["schemas"]["SessionResponse"];
export type Node = components["schemas"]["NodeResponse"];
export type Run = components["schemas"]["RunResponse"];
export type RunSummary = components["schemas"]["RunSummaryResponse"];
export type Diff = components["schemas"]["DiffResponse"];
export type Graph = components["schemas"]["GraphResponse"];
export type NodeDependency = components["schemas"]["NodeDependencyResponse"];
export type UpdateNode = components["schemas"]["UpdateNodeRequest"];
export type AcceptanceResult =
  components["schemas"]["AcceptanceResultResponse"];
export type CriterionOutcome = components["schemas"]["CriterionOutcome"];
export type Merge = components["schemas"]["MergeResponse"];
export type NodeReview = components["schemas"]["NodeReviewResponse"];
export type RunOutcome = components["schemas"]["RunOutcomeResponse"];

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

function graphPath(sessionId: string): string {
  return `/api/graphs/${encodeURIComponent(sessionId)}`;
}

function nodePath(sessionId: string, nodeId: string): string {
  return `${sessionPath(sessionId)}/nodes/${encodeURIComponent(nodeId)}`;
}

function nodeRunPath(sessionId: string, nodeId: string, runId: string): string {
  return `${nodePath(sessionId, nodeId)}/runs/${encodeURIComponent(runId)}`;
}

function jsonBody(body: unknown): Pick<RequestInit, "body" | "headers"> {
  return {
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

export const api = {
  listSessions: () => request<readonly Session[]>("/api/sessions"),
  getSession: (sessionId: string) => request<Session>(sessionPath(sessionId)),
  getGraph: (sessionId: string) => request<Graph>(graphPath(sessionId)),
  approveGraph: (sessionId: string) =>
    request<Graph>(`${graphPath(sessionId)}/approve`, { method: "POST" }),
  runGraph: (sessionId: string) =>
    request<unknown>(`${graphPath(sessionId)}/runs`, { method: "POST" }),
  updateNode: (sessionId: string, nodeId: string, body: UpdateNode) =>
    request<Node>(nodePath(sessionId, nodeId), {
      method: "PUT",
      ...jsonBody(body),
    }),
  deleteNode: (sessionId: string, nodeId: string) =>
    request<Graph>(nodePath(sessionId, nodeId), { method: "DELETE" }),
  addDependency: (sessionId: string, nodeId: string, dependsOnId: string) =>
    request<Graph>(
      `${nodePath(sessionId, nodeId)}/dependencies/${encodeURIComponent(dependsOnId)}`,
      { method: "PUT" },
    ),
  removeDependency: (sessionId: string, nodeId: string, dependsOnId: string) =>
    request<Graph>(
      `${nodePath(sessionId, nodeId)}/dependencies/${encodeURIComponent(dependsOnId)}`,
      { method: "DELETE" },
    ),
  listNodeRuns: (sessionId: string, nodeId: string) =>
    request<readonly Run[]>(`${nodePath(sessionId, nodeId)}/runs`),
  getNodeRunSummary: (sessionId: string, nodeId: string, runId: string) =>
    request<RunSummary>(`${nodeRunPath(sessionId, nodeId, runId)}/summary`),
  getNodeRunEvents: (sessionId: string, nodeId: string, runId: string) =>
    request<readonly AgentEvent[]>(
      `${nodeRunPath(sessionId, nodeId, runId)}/events`,
    ),
  getNodeDiff: (sessionId: string, nodeId: string) =>
    request<Diff>(`${nodePath(sessionId, nodeId)}/diff`),
  listNodeAcceptance: (sessionId: string, nodeId: string, attempt?: number) =>
    request<readonly AcceptanceResult[]>(
      `${nodePath(sessionId, nodeId)}/acceptance${attempt === undefined ? "" : `?attempt=${attempt}`}`,
    ),
  runNode: (sessionId: string, nodeId: string) =>
    request<RunOutcome>(`${nodePath(sessionId, nodeId)}/runs`, {
      method: "POST",
    }),
  killNode: (sessionId: string, nodeId: string) =>
    request<Run>(`${nodePath(sessionId, nodeId)}/kill`, { method: "POST" }),
  retryNode: (sessionId: string, nodeId: string, feedback?: string) =>
    request<RunOutcome>(`${nodePath(sessionId, nodeId)}/retry`, {
      method: "POST",
      ...(feedback === undefined ? {} : jsonBody({ feedback })),
    }),
  approveNode: (
    sessionId: string,
    nodeId: string,
    outcomes: Readonly<Record<number, CriterionOutcome>>,
  ) =>
    request<Merge>(`${nodePath(sessionId, nodeId)}/approve`, {
      method: "POST",
      ...jsonBody({ outcomes }),
    }),
  rejectNode: (
    sessionId: string,
    nodeId: string,
    feedback: string,
    outcomes: Readonly<Record<number, CriterionOutcome>>,
  ) =>
    request<NodeReview>(`${nodePath(sessionId, nodeId)}/reject`, {
      method: "POST",
      ...jsonBody({ feedback, outcomes }),
    }),
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
