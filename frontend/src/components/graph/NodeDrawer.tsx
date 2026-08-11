import { X } from "lucide-react";
import type {
  AcceptanceResult,
  CriterionOutcome,
  Node,
  Run,
  RunSummary,
} from "@/api/client";
import type { AgentEvent } from "@/api/events";
import { NodeReviewPanel } from "@/components/graph/NodeReviewPanel";
import {
  NodeStatePanel,
  type PendingNodeAction,
} from "@/components/graph/NodeStatePanel";
import { Button } from "@/components/ui/button";
import { nodeStateVisual } from "@/lib/node-state";
import { cn } from "@/lib/utils";

type Props = {
  node: Node;
  dependencies: readonly string[];
  runs: readonly Run[];
  events: readonly AgentEvent[];
  summary: RunSummary | null;
  patch: string;
  acceptance: readonly AcceptanceResult[];
  pendingAction: PendingNodeAction;
  error: string | null;
  onClose: () => void;
  onRun: () => void;
  onKill: () => void;
  onRetry: (feedback?: string) => void;
  onApprove: (outcomes: Readonly<Record<number, CriterionOutcome>>) => void;
  onReject: (
    feedback: string,
    outcomes: Readonly<Record<number, CriterionOutcome>>,
  ) => void;
};

export function NodeDrawer(props: Props) {
  const { node } = props;
  const visual = nodeStateVisual(node.status);
  const StatusIcon = visual.icon;
  const latest = props.runs.at(-1);
  return (
    <aside className="flex w-[480px] max-w-[60vw] shrink-0 flex-col border-border border-l bg-elevated shadow-2xl">
      <div className="flex h-7 items-center border-border border-b bg-inset/55 px-3 font-mono text-badge uppercase tracking-[0.12em] text-fg-subtle">
        Worktree inspector
      </div>
      <header className="flex min-h-12 items-center gap-2 border-border border-b px-3 py-2">
        <StatusIcon
          className={cn(
            "size-4",
            visual.text,
            node.status === "running" && "animate-spin",
          )}
          data-motion={node.status === "running" ? "essential" : undefined}
        />
        <div className="min-w-0 flex-1">
          <h2 className="truncate font-semibold text-ui">{node.name}</h2>
          <span className={cn("text-meta", visual.text)}>{visual.label}</span>
          {latest ? (
            <span className="ml-2 text-meta text-fg-muted">
              attempt {latest.attempt}
            </span>
          ) : null}
        </div>
        <Button
          aria-label="Close node drawer"
          onClick={props.onClose}
          size="icon-sm"
          variant="ghost"
        >
          <X />
        </Button>
      </header>
      <div className="grid grid-cols-2 gap-px border-border border-b bg-border text-badge">
        <div className="bg-surface px-3 py-1.5">
          <span className="text-fg-subtle">Harness </span>
          <code>{node.harness}</code>
        </div>
        <div className="bg-surface px-3 py-1.5">
          <span className="text-fg-subtle">Model </span>
          <code>{node.model ?? "default"}</code>
        </div>
      </div>
      {props.error ? (
        <p
          className="border-failed border-b bg-failed/10 px-3 py-2 text-meta text-failed"
          role="alert"
        >
          {props.error}
        </p>
      ) : null}
      {node.status === "awaiting_review" ? (
        <NodeReviewPanel
          acceptance={props.acceptance}
          busy={props.pendingAction !== null}
          onApprove={props.onApprove}
          onReject={props.onReject}
          patch={props.patch}
        />
      ) : null}
      {node.status !== "awaiting_review" ? (
        <NodeStatePanel
          dependencies={props.dependencies}
          events={props.events}
          node={node}
          onKill={props.onKill}
          onRetry={props.onRetry}
          onRun={props.onRun}
          patch={props.patch}
          pendingAction={props.pendingAction}
          runs={props.runs}
          summary={props.summary}
        />
      ) : null}
    </aside>
  );
}
