import { Eye, Play, RotateCcw, Square } from "lucide-react";
import type { Node } from "@/api/client";
import { Button } from "@/components/ui/button";

type Action = "approve" | "kill" | "retry" | "start";

type Props = {
  node: Node;
  pendingAction: Action | null;
  onAction: (action: Action) => void;
};

export function SessionActions({ node, pendingAction, onAction }: Props) {
  const busy = pendingAction !== null;
  if (node.status === "ready") {
    return (
      <Button disabled={busy} onClick={() => onAction("start")}>
        <Play data-icon="inline-start" />
        {pendingAction === "start" ? "Starting…" : "Start run"}
      </Button>
    );
  }
  if (node.status === "running") {
    return (
      <Button
        disabled={busy}
        variant="destructive"
        onClick={() => onAction("kill")}
      >
        <Square data-icon="inline-start" />
        {pendingAction === "kill" ? "Stopping…" : "Kill run"}
      </Button>
    );
  }
  if (node.status === "failed" || node.status === "blocked") {
    return (
      <Button disabled={busy} onClick={() => onAction("retry")}>
        <RotateCcw data-icon="inline-start" />
        {pendingAction === "retry" ? "Retrying…" : "Retry"}
      </Button>
    );
  }
  if (node.status === "awaiting_review") {
    return (
      <Button disabled={busy} onClick={() => onAction("approve")}>
        <Eye data-icon="inline-start" />
        {pendingAction === "approve" ? "Approving…" : "Approve merge"}
      </Button>
    );
  }
  return null;
}

export type { Action as SessionAction };
