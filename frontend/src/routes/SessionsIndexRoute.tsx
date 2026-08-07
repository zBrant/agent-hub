import { Workflow } from "lucide-react";
import { EmptyState } from "@/components/layout/EmptyState";

export function SessionsIndexRoute() {
  return (
    <EmptyState
      icon={Workflow}
      title="No sessions yet"
      description="A session holds the graph, its worktrees and its integration branch. Creating one needs the orchestrator API."
    />
  );
}
