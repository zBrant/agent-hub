import { Handle, type Node, type NodeProps, Position } from "@xyflow/react";
import { memo } from "react";
import type { Node as AgentNode } from "@/api/client";
import { harnessDotClass } from "@/lib/harness";
import { nodeStateVisual } from "@/lib/node-state";
import { cn } from "@/lib/utils";

export type GraphNodeData = {
  editable: boolean;
  node: AgentNode;
};

export type ActivityFlowNode = Node<GraphNodeData, "activity">;

function GraphNodeComponent({ data, selected }: NodeProps<ActivityFlowNode>) {
  const visual = nodeStateVisual(data.node.status);
  const StatusIcon = visual.icon;
  return (
    <div
      className={cn(
        "relative w-60 overflow-hidden border border-border bg-surface text-fg shadow-xl",
        selected && "border-accent ring-1 ring-accent/40",
      )}
    >
      <span className={cn("absolute inset-y-0 left-0 w-[3px]", visual.fill)} />
      <Handle
        className="size-2! rounded-[1px]! border-border-strong! bg-elevated! opacity-0 transition-opacity group-hover:opacity-100"
        isConnectable={data.editable}
        position={Position.Top}
        type="target"
      />
      <div className="flex min-w-0 items-center gap-2 px-3 pt-2.5 pb-1">
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
        <span className="inline-flex shrink-0 items-center gap-1 font-mono text-badge text-fg-muted">
          <span
            aria-hidden="true"
            className={cn(
              "size-1.5 rounded-full",
              harnessDotClass(data.node.harness),
            )}
          />
          {data.node.harness}
        </span>
      </div>
      <div className="flex items-center justify-between gap-2 px-3 pt-1 pb-2.5 text-meta text-fg-muted">
        <span className="inline-flex items-center gap-1">{visual.label}</span>
        <code className="truncate text-code">
          {data.node.model ?? "default model"}
        </code>
      </div>
      <Handle
        className="size-2! rounded-[1px]! border-border-strong! bg-elevated!"
        isConnectable={data.editable}
        position={Position.Bottom}
        type="source"
      />
    </div>
  );
}

export const GraphNode = memo(GraphNodeComponent);
