import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Loader } from "lucide-react";
import { lazy, Suspense, useEffect } from "react";
import { Link, useParams } from "react-router";
import {
  ApiError,
  api,
  type Graph,
  type Node,
  type UpdateNode,
} from "@/api/client";
import { DiffView } from "@/components/session/DiffView";
import { EventFeed } from "@/components/session/EventFeed";
import {
  type SessionAction,
  SessionActions,
} from "@/components/session/SessionActions";
import { TokenSummary } from "@/components/session/TokenSummary";
import { nodeStateVisual } from "@/lib/node-state";
import { cn } from "@/lib/utils";
import { useSessionFeedStore } from "@/stores/session-feed-store";
import { graphTopic, sessionTopic } from "@/ws/protocol";
import { useWebSocketClient } from "@/ws/WebSocketProvider";

const EMPTY_EVENTS = [] as const;

const GraphWorkspace = lazy(async () => {
  const module = await import("@/components/graph/GraphWorkspace");
  return { default: module.GraphWorkspace };
});

const NodeDrawerRoute = lazy(async () => {
  const module = await import("@/routes/NodeDrawerRoute");
  return { default: module.NodeDrawerRoute };
});

type GraphOperation =
  | { kind: "update_node"; nodeId: string; update: UpdateNode }
  | { kind: "delete_node"; nodeId: string }
  | { kind: "add_dependency"; nodeId: string; dependsOnId: string }
  | { kind: "remove_dependency"; nodeId: string; dependsOnId: string }
  | { kind: "approve" };

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected local API error";
}

export function SessionRoute() {
  const { id } = useParams();
  const queryClient = useQueryClient();
  const websocket = useWebSocketClient();
  const append = useSessionFeedStore((state) => state.append);
  const hydrate = useSessionFeedStore((state) => state.hydrate);

  const graph = useQuery({
    queryKey: ["graph", id],
    queryFn: () => api.getGraph(id ?? ""),
    enabled: Boolean(id),
  });
  const legacySingleNode = Boolean(
    graph.data?.nodes.length === 1 && graph.data.nodes[0]?.status !== "pending",
  );
  const session = useQuery({
    queryKey: ["session", id],
    queryFn: () => api.getSession(id ?? ""),
    enabled: Boolean(id && legacySingleNode),
  });
  const node = useQuery({
    queryKey: ["session", id, "node"],
    queryFn: () => api.getNode(id ?? ""),
    enabled: Boolean(id && legacySingleNode),
  });
  const runs = useQuery({
    queryKey: ["session", id, "runs"],
    queryFn: () => api.listRuns(id ?? ""),
    enabled: Boolean(id && legacySingleNode),
  });
  const latest = runs.data?.at(-1);
  const summary = useQuery({
    queryKey: ["session", id, "run", latest?.id, "summary"],
    queryFn: () => api.getRunSummary(id ?? "", latest?.id ?? ""),
    enabled: Boolean(id && latest),
  });
  const persistedEvents = useQuery({
    queryKey: ["session", id, "run", latest?.id, "events"],
    queryFn: () => api.getRunEvents(id ?? "", latest?.id ?? ""),
    enabled: Boolean(id && latest),
  });
  const diff = useQuery({
    queryKey: ["session", id, "diff"],
    queryFn: () => api.getDiff(id ?? ""),
    enabled: Boolean(id && legacySingleNode),
  });
  const events = useSessionFeedStore((state) =>
    latest ? (state.eventsByRun[latest.id] ?? EMPTY_EVENTS) : EMPTY_EVENTS,
  );

  useEffect(() => {
    if (latest && persistedEvents.data) {
      hydrate(latest.id, persistedEvents.data);
    }
  }, [hydrate, latest, persistedEvents.data]);

  useEffect(() => {
    if (!id || !websocket || !legacySingleNode) return;
    return websocket.subscribe(sessionTopic(id), (event) => {
      if (!("run_id" in event)) return;
      append(event);
      if (event.type === "usage") {
        void queryClient.invalidateQueries({
          queryKey: ["session", id, "run", event.run_id, "summary"],
        });
      }
      if (event.type === "run_started" || event.type === "run_finished") {
        void Promise.all([
          queryClient.invalidateQueries({
            queryKey: ["session", id],
            exact: true,
          }),
          queryClient.invalidateQueries({
            queryKey: ["session", id, "node"],
            exact: true,
          }),
          queryClient.invalidateQueries({
            queryKey: ["session", id, "runs"],
            exact: true,
          }),
          queryClient.invalidateQueries({
            queryKey: ["session", id, "diff"],
            exact: true,
          }),
        ]);
      }
    });
  }, [append, id, legacySingleNode, queryClient, websocket]);

  useEffect(() => {
    if (!id || !websocket) return;
    return websocket.subscribe(graphTopic(id), (_, frame) => {
      if (frame.type !== "node_status") return;
      void queryClient.invalidateQueries({ queryKey: ["graph", id] });
    });
  }, [id, queryClient, websocket]);

  const graphAction = useMutation({
    mutationFn: async (operation: GraphOperation): Promise<Graph | Node> => {
      if (!id) throw new Error("Missing session id");
      switch (operation.kind) {
        case "update_node":
          return api.updateNode(id, operation.nodeId, operation.update);
        case "delete_node":
          return api.deleteNode(id, operation.nodeId);
        case "add_dependency":
          return api.addDependency(id, operation.nodeId, operation.dependsOnId);
        case "remove_dependency":
          return api.removeDependency(
            id,
            operation.nodeId,
            operation.dependsOnId,
          );
        case "approve":
          return api.approveGraph(id);
      }
    },
    onSuccess: (result) => {
      queryClient.setQueryData<Graph>(["graph", id], (current) => {
        if ("nodes" in result) return result;
        if (!current) return current;
        return {
          ...current,
          nodes: current.nodes.map((node) =>
            node.id === result.id ? result : node,
          ),
        };
      });
    },
  });

  const action = useMutation({
    mutationFn: async (kind: SessionAction) => {
      if (!id) throw new Error("Missing session id");
      switch (kind) {
        case "start":
          return api.start(id);
        case "kill":
          return api.kill(id);
        case "retry":
          return api.retry(id);
        case "approve":
          return api.approve(id);
      }
    },
    onSettled: async () => {
      await queryClient.invalidateQueries({ queryKey: ["session", id] });
    },
  });

  if (!id) return <RouteError title="Missing session id" />;
  if (graph.data) {
    const proposal = graph.data.nodes.every(
      (graphNode) => graphNode.status === "pending",
    );
    if (proposal || graph.data.nodes.length !== 1) {
      return (
        <Suspense
          fallback={
            <div className="flex h-full items-center justify-center gap-2 text-meta text-fg-muted">
              <Loader className="size-4 animate-spin" data-motion="essential" />
              Loading graph canvas…
            </div>
          }
        >
          <GraphWorkspace
            graph={graph.data}
            onAddDependency={async (nodeId, dependsOnId) => {
              await graphAction.mutateAsync({
                kind: "add_dependency",
                nodeId,
                dependsOnId,
              });
            }}
            onApprove={async () => {
              await graphAction.mutateAsync({ kind: "approve" });
            }}
            onDeleteNode={async (nodeId) => {
              await graphAction.mutateAsync({ kind: "delete_node", nodeId });
            }}
            onRemoveDependency={async (nodeId, dependsOnId) => {
              await graphAction.mutateAsync({
                kind: "remove_dependency",
                nodeId,
                dependsOnId,
              });
            }}
            onUpdateNode={async (nodeId, update) => {
              await graphAction.mutateAsync({
                kind: "update_node",
                nodeId,
                update,
              });
            }}
            renderNodeDrawer={(selectedNode, onClose) => {
              const dependencies = graph.data.edges
                .filter((edge) => edge.node_id === selectedNode.id)
                .map(
                  (edge) =>
                    graph.data.nodes.find(
                      (candidate) => candidate.id === edge.depends_on_id,
                    )?.name ?? edge.depends_on_id,
                );
              return (
                <Suspense
                  fallback={
                    <aside className="flex w-[480px] max-w-[60vw] items-center justify-center border-border border-l bg-elevated text-meta text-fg-muted">
                      Loading node…
                    </aside>
                  }
                >
                  <NodeDrawerRoute
                    key={selectedNode.id}
                    dependencies={dependencies}
                    node={selectedNode}
                    onClose={onClose}
                    onDeleteNode={async (nodeId) => {
                      await graphAction.mutateAsync({
                        kind: "delete_node",
                        nodeId,
                      });
                    }}
                    onUpdateNode={async (nodeId, update) => {
                      await graphAction.mutateAsync({
                        kind: "update_node",
                        nodeId,
                        update,
                      });
                    }}
                    sessionId={id}
                  />
                </Suspense>
              );
            }}
          />
        </Suspense>
      );
    }
  }
  if (
    session.isLoading ||
    graph.isLoading ||
    node.isLoading ||
    runs.isLoading
  ) {
    return (
      <div className="flex h-full items-center justify-center gap-2 text-meta text-fg-muted">
        <Loader className="size-4 animate-spin" data-motion="essential" />
        Loading persisted session…
      </div>
    );
  }
  const loadError = session.error ?? graph.error ?? node.error ?? runs.error;
  if (loadError || !session.data || !graph.data || !node.data) {
    return (
      <RouteError
        title={
          loadError instanceof ApiError && loadError.status === 404
            ? "Session not found"
            : "Could not load session"
        }
        detail={loadError ? message(loadError) : undefined}
      />
    );
  }

  const visual = nodeStateVisual(node.data.status);
  const StatusIcon = visual.icon;
  const terminal = latest && latest.status !== "running";
  const unsafe = Boolean(terminal && summary.data && !summary.data.trusted);

  return (
    <div className="flex min-h-full flex-col bg-bg">
      <header className="border-border border-b bg-surface px-4 py-3">
        <div className="flex items-start gap-3">
          <Link
            aria-label="All sessions"
            className="mt-1 text-fg-muted hover:text-fg"
            to="/sessions"
          >
            <ArrowLeft className="size-4" />
          </Link>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="truncate font-semibold text-title">
                {session.data.title}
              </h1>
              <span
                className={cn(
                  "inline-flex items-center gap-1 rounded-sm border border-border bg-elevated px-1.5 py-0.5 text-badge",
                  visual.text,
                )}
              >
                <StatusIcon
                  className={cn(
                    "size-3",
                    node.data.status === "running" && "animate-spin",
                  )}
                  data-motion={
                    node.data.status === "running" ? "essential" : undefined
                  }
                />
                {visual.label}
              </span>
              {unsafe ? (
                <span className="inline-flex items-center gap-1 rounded-sm border border-blocked px-1.5 py-0.5 text-badge text-blocked">
                  <AlertTriangle className="size-3" /> Parser drift — unsafe
                </span>
              ) : null}
            </div>
            <div className="mt-1 flex flex-wrap gap-x-3 text-meta text-fg-muted">
              <code className="text-code">{session.data.id}</code>
              <span>{node.data.harness}</span>
              <code className="text-code">
                {node.data.model ?? "default model"}
              </code>
              {latest ? <span>attempt {latest.attempt}</span> : null}
            </div>
          </div>
          <SessionActions
            node={node.data}
            pendingAction={action.isPending ? action.variables : null}
            onAction={(kind) => action.mutate(kind)}
          />
        </div>
        {action.error ? (
          <p className="mt-2 text-meta text-failed">{message(action.error)}</p>
        ) : null}
      </header>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1.55fr)_minmax(320px,0.85fr)]">
        <div className="flex min-h-[420px] min-w-0 flex-col border-border lg:border-r">
          <EventFeed events={events} />
        </div>
        <aside className="flex min-h-[420px] min-w-0 flex-col bg-surface">
          <TokenSummary summary={summary.data ?? null} />
          <RunHistory runs={runs.data ?? EMPTY_EVENTS} />
          <DiffView patch={diff.data?.patch ?? ""} />
        </aside>
      </div>
    </div>
  );
}

function RunHistory({
  runs,
}: {
  runs: readonly { id: string; attempt: number; status: string }[];
}) {
  return (
    <section className="border-border border-b px-3 py-2">
      <h2 className="mb-1 font-semibold text-ui">Run history</h2>
      {runs.length === 0 ? (
        <p className="text-meta text-fg-muted">No attempts yet.</p>
      ) : (
        <ol className="space-y-1">
          {runs.map((run) => (
            <li
              key={run.id}
              className="flex items-center justify-between gap-2 text-meta"
            >
              <code className="truncate text-code text-fg-muted">{run.id}</code>
              <span className="shrink-0">
                #{run.attempt} · {run.status}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function RouteError({
  title,
  detail,
}: {
  title: string;
  detail?: string | undefined;
}) {
  return (
    <div className="flex h-full items-center justify-center p-4 text-center">
      <div>
        <AlertTriangle className="mx-auto mb-2 size-5 text-failed" />
        <h1 className="font-semibold text-ui">{title}</h1>
        {detail ? (
          <p className="mt-1 text-meta text-fg-muted">{detail}</p>
        ) : null}
        <Link
          className="mt-3 inline-block text-meta text-accent hover:text-accent-hover"
          to="/sessions"
        >
          Back to sessions
        </Link>
      </div>
    </div>
  );
}
