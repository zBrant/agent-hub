import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { memo } from "react";
import type { Node as AgentNode } from "@/api/client";
import { nodeStateVisual } from "@/lib/node-state";
import { cn } from "@/lib/utils";

export type GraphNodeData = {
  editable: boolean;
  node: AgentNode;
};

export type ActivityFlowNode = Node<GraphNodeData, "activity">;

function harnessDot(harness: string): string {
  switch (harness) {
    case "claude-code":
      return "bg-harness-claude-code";
    case "codex":
      return "bg-harness-codex";
    case "opencode":
      return "bg-harness-opencode";
    default:
      return "bg-fg-subtle";
  }
}

function GraphNodeComponent({ data, selected }: NodeProps<ActivityFlowNode>) {
  const visual = nodeStateVisual(data.node.status);
  const StatusIcon = visual.icon;
  return (
    <div
      className={cn(
        "w-60 rounded-md border-[1.5px] bg-surface px-3 py-2 text-fg",
        visual.border,
        selected && "border-2 border-accent",
      )}
    >
      <Handle
        className="size-2! border-border-strong! bg-elevated!"
        isConnectable={data.editable}
        position={Position.Top}
        type="target"
      />
      <div className="flex min-w-0 items-center gap-2">
        <StatusIcon
          aria-hidden="true"
          className={cn(
            "size-4 shrink-0",
            visual.text,
            data.node.status === "running" && "animate-spin",
          )}
          data-motion={data.node.status === "running" ? "essential" : undefined}
        />
        <span className="min-w-0 flex-1 truncate font-semibold text-ui">
          {data.node.name}
        </span>
        <span className="inline-flex shrink-0 items-center gap-1 rounded-sm border border-border bg-elevated px-1.5 py-0.5 font-mono text-badge">
          <span
            aria-hidden="true"
            className={cn(
              "size-1.5 rounded-full",
              harnessDot(data.node.harness),
            )}
          />
          {data.node.harness}
        </span>
      </div>
      <div className="mt-1 flex items-center justify-between gap-2 text-meta text-fg-muted">
        <span className="inline-flex items-center gap-1">
          <span className={cn("size-1.5 rounded-full", visual.fill)} />
          {visual.label}
        </span>
        <code className="truncate text-code">
          {data.node.model ?? "default model"}
        </code>
      </div>
      <Handle
        className="size-2! border-border-strong! bg-elevated!"
        isConnectable={data.editable}
        position={Position.Bottom}
        type="source"
      />
    </div>
  );
}

export const GraphNode = memo(GraphNodeComponent);
