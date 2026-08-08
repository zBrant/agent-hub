import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { api, type Node } from "@/api/client";
import { useSessionFeedStore } from "@/stores/session-feed-store";
import { graphTopic, runTopic, sessionTopic } from "@/ws/protocol";
import { useWebSocketClient } from "@/ws/WebSocketProvider";

const EMPTY = [] as const;

export function useNodeRunData(sessionId: string, node: Node) {
  const queryClient = useQueryClient();
  const websocket = useWebSocketClient();
  const append = useSessionFeedStore((state) => state.append);
  const hydrate = useSessionFeedStore((state) => state.hydrate);
  const runs = useQuery({
    queryKey: ["node", sessionId, node.id, "runs"],
    queryFn: () => api.listNodeRuns(sessionId, node.id),
    enabled: node.status !== "pending",
  });
  const latest = runs.data?.at(-1);
  const summary = useQuery({
    queryKey: ["node", sessionId, node.id, "run", latest?.id, "summary"],
    queryFn: () => api.getNodeRunSummary(sessionId, node.id, latest?.id ?? ""),
    enabled: Boolean(latest),
  });
  const persistedEvents = useQuery({
    queryKey: ["node", sessionId, node.id, "run", latest?.id, "events"],
    queryFn: () => api.getNodeRunEvents(sessionId, node.id, latest?.id ?? ""),
    enabled: Boolean(latest),
  });
  const diff = useQuery({
    queryKey: ["node", sessionId, node.id, "diff"],
    queryFn: () => api.getNodeDiff(sessionId, node.id),
    enabled: ["awaiting_review", "blocked", "done", "failed"].includes(
      node.status,
    ),
  });
  const acceptance = useQuery({
    queryKey: ["node", sessionId, node.id, "acceptance", latest?.attempt],
    queryFn: () => api.listNodeAcceptance(sessionId, node.id, latest?.attempt),
    enabled: node.status === "awaiting_review" && Boolean(latest),
  });
  const events = useSessionFeedStore((state) =>
    latest ? (state.eventsByRun[latest.id] ?? EMPTY) : EMPTY,
  );

  useEffect(() => {
    if (latest && persistedEvents.data) {
      hydrate(latest.id, persistedEvents.data);
    }
  }, [hydrate, latest, persistedEvents.data]);

  useEffect(() => {
    if (!websocket || node.status === "pending") return;
    return websocket.subscribe(graphTopic(sessionId), (payload, frame) => {
      if (frame.type !== "node_status" || !("node_id" in payload)) return;
      if (payload.node_id !== node.id) return;
      void Promise.all([
        queryClient.invalidateQueries({
          queryKey: ["node", sessionId, node.id, "runs"],
        }),
        queryClient.invalidateQueries({
          queryKey: ["node", sessionId, node.id, "diff"],
        }),
      ]);
    });
  }, [node.id, node.status, queryClient, sessionId, websocket]);

  useEffect(() => {
    if (!websocket || node.status === "pending") return;
    return websocket.subscribe(sessionTopic(sessionId), (payload, frame) => {
      if (frame.type !== "event" || !("run_id" in payload)) return;
      if (payload.type !== "run_started" && payload.type !== "run_finished") {
        return;
      }
      void queryClient.invalidateQueries({
        queryKey: ["node", sessionId, node.id, "runs"],
      });
    });
  }, [node.id, node.status, queryClient, sessionId, websocket]);

  useEffect(() => {
    if (!websocket || !latest) return;
    return websocket.subscribe(runTopic(latest.id), (payload, frame) => {
      if (frame.type !== "event" || !("run_id" in payload)) return;
      append(payload);
      if (payload.type === "usage") {
        void queryClient.invalidateQueries({
          queryKey: ["node", sessionId, node.id, "run", latest.id, "summary"],
        });
      }
      if (payload.type === "run_finished") {
        void queryClient.invalidateQueries({
          queryKey: ["node", sessionId, node.id, "runs"],
        });
      }
    });
  }, [append, latest, node.id, queryClient, sessionId, websocket]);

  return {
    acceptance: acceptance.data ?? EMPTY,
    events,
    patch: diff.data?.patch ?? "",
    runs: runs.data ?? EMPTY,
    summary: summary.data ?? null,
    error:
      runs.error ??
      summary.error ??
      persistedEvents.error ??
      diff.error ??
      acceptance.error,
  };
}
