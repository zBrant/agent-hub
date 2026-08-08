import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  api,
  type CriterionOutcome,
  type Node,
  type UpdateNode,
} from "@/api/client";
import { NodeDrawer } from "@/components/graph/NodeDrawer";
import { NodeEditor } from "@/components/graph/NodeEditor";
import { useNodeRunData } from "@/routes/useNodeRunData";

type Props = {
  sessionId: string;
  node: Node;
  dependencies: readonly string[];
  onClose: () => void;
  onUpdateNode: (nodeId: string, update: UpdateNode) => Promise<void>;
  onDeleteNode: (nodeId: string) => Promise<void>;
};

type NodeAction =
  | { kind: "run" }
  | { kind: "kill" }
  | { kind: "retry" }
  | {
      kind: "approve";
      outcomes: Readonly<Record<number, CriterionOutcome>>;
    }
  | {
      kind: "reject";
      feedback: string;
      outcomes: Readonly<Record<number, CriterionOutcome>>;
    };

function message(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected local API error";
}

export function NodeDrawerRoute({
  sessionId,
  node,
  dependencies,
  onClose,
  onUpdateNode,
  onDeleteNode,
}: Props) {
  const queryClient = useQueryClient();
  const data = useNodeRunData(sessionId, node);

  const action = useMutation({
    mutationFn: (operation: NodeAction) => {
      switch (operation.kind) {
        case "run":
          return api.runGraph(sessionId);
        case "kill":
          return api.killNode(sessionId, node.id);
        case "retry":
          return api.retryNode(sessionId, node.id);
        case "approve":
          return api.approveNode(sessionId, node.id, operation.outcomes);
        case "reject":
          return api.rejectNode(
            sessionId,
            node.id,
            operation.feedback,
            operation.outcomes,
          );
      }
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["graph", sessionId] }),
        queryClient.invalidateQueries({
          queryKey: ["node", sessionId, node.id],
        }),
      ]);
    },
  });

  if (node.status === "pending") {
    return (
      <NodeEditor
        busy={action.isPending}
        dependencies={dependencies}
        node={node}
        onClose={onClose}
        onRemove={(nodeId) => void onDeleteNode(nodeId)}
        onSave={(nodeId, update) => void onUpdateNode(nodeId, update)}
      />
    );
  }

  return (
    <NodeDrawer
      acceptance={data.acceptance}
      dependencies={dependencies}
      error={
        action.error || data.error ? message(action.error ?? data.error) : null
      }
      events={data.events}
      node={node}
      onApprove={(outcomes) => action.mutate({ kind: "approve", outcomes })}
      onClose={onClose}
      onKill={() => action.mutate({ kind: "kill" })}
      onReject={(feedback, outcomes) =>
        action.mutate({ kind: "reject", feedback, outcomes })
      }
      onRetry={() => action.mutate({ kind: "retry" })}
      onRun={() => action.mutate({ kind: "run" })}
      patch={data.patch}
      pendingAction={action.isPending ? (action.variables?.kind ?? null) : null}
      runs={data.runs}
      summary={data.summary}
    />
  );
}
