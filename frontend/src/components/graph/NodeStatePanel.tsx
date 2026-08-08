import { Play, RotateCcw, Square } from "lucide-react";
import type { Node, Run, RunSummary } from "@/api/client";
import type { AgentEvent } from "@/api/events";
import { DiffView } from "@/components/session/DiffView";
import { EventFeed } from "@/components/session/EventFeed";
import { TokenSummary } from "@/components/session/TokenSummary";
import { Button } from "@/components/ui/button";

export type PendingNodeAction =
  | "run"
  | "kill"
  | "retry"
  | "approve"
  | "reject"
  | null;

type Props = {
  node: Node;
  dependencies: readonly string[];
  runs: readonly Run[];
  events: readonly AgentEvent[];
  summary: RunSummary | null;
  patch: string;
  pendingAction: PendingNodeAction;
  onRun: () => void;
  onKill: () => void;
  onRetry: () => void;
};

function blockedReason(latest: Run | undefined, events: readonly AgentEvent[]) {
  if (!latest) {
    return "The dependencies could not be combined into this node's worktree.";
  }
  if (latest.status === "interrupted") {
    return "The previous process could not be safely adopted. Retry only after confirming it has stopped.";
  }
  if (events.some((event) => event.type === "permission")) {
    return "The run encountered a permission gate. Review the event feed before retrying.";
  }
  return "The checkpoint could not be integrated, usually because of a merge conflict.";
}

function keyedCriteria(criteria: readonly string[]) {
  const occurrences = new Map<string, number>();
  return criteria.map((criterion) => {
    const occurrence = (occurrences.get(criterion) ?? 0) + 1;
    occurrences.set(criterion, occurrence);
    return { criterion, key: `${criterion}:${occurrence}` };
  });
}

function Settings({
  node,
  dependencies,
}: Pick<Props, "node" | "dependencies">) {
  return (
    <section className="space-y-3 border-border border-b p-3">
      <div>
        <h3 className="text-meta text-fg-muted">Prompt</h3>
        <p className="mt-1 whitespace-pre-wrap text-ui">{node.prompt}</p>
      </div>
      <div className="grid grid-cols-2 gap-3 text-meta">
        <div>
          <span className="text-fg-muted">Harness</span>
          <code className="mt-1 block text-code">{node.harness}</code>
        </div>
        <div>
          <span className="text-fg-muted">Model</span>
          <code className="mt-1 block text-code">
            {node.model ?? "default model"}
          </code>
        </div>
      </div>
      <div>
        <h3 className="text-meta text-fg-muted">Dependencies</h3>
        <p className="mt-1 text-ui">
          {dependencies.length > 0 ? dependencies.join(", ") : "None"}
        </p>
      </div>
      <div>
        <h3 className="text-meta text-fg-muted">Acceptance criteria</h3>
        {node.acceptance_criteria.length > 0 ? (
          <ul className="mt-1 list-inside list-disc space-y-1 text-ui">
            {keyedCriteria(node.acceptance_criteria).map(
              ({ criterion, key }) => (
                <li key={key}>{criterion}</li>
              ),
            )}
          </ul>
        ) : (
          <p className="mt-1 text-ui">None</p>
        )}
      </div>
    </section>
  );
}

export function NodeStatePanel(props: Props) {
  const busy = props.pendingAction !== null;
  const latest = props.runs.at(-1);
  switch (props.node.status) {
    case "ready":
      return (
        <>
          <Settings dependencies={props.dependencies} node={props.node} />
          <div className="p-3">
            <p className="mb-3 text-meta text-fg-muted">
              The graph is approved, so its authored fields are now locked. The
              scheduler will start every currently ready node.
            </p>
            <Button disabled={busy} onClick={props.onRun} size="sm">
              <Play /> Run ready nodes
            </Button>
          </div>
        </>
      );
    case "running":
      return (
        <>
          <div className="flex min-h-0 flex-1 flex-col">
            <EventFeed events={props.events} />
          </div>
          <div className="flex items-center justify-between gap-3 border-border border-t p-3">
            <p className="text-meta text-fg-muted">
              Messaging and terminal attach are unavailable for this runtime.
            </p>
            <Button
              disabled={props.pendingAction === "kill"}
              onClick={props.onKill}
              size="sm"
              variant="destructive"
            >
              <Square /> Kill
            </Button>
          </div>
        </>
      );
    case "blocked":
      return (
        <>
          <p className="border-border border-b p-3 text-ui">
            {blockedReason(latest, props.events)}
          </p>
          <div className="min-h-0 flex-1">
            <DiffView patch={props.patch} />
          </div>
          <div className="border-border border-t p-3">
            <Button disabled={busy} onClick={props.onRetry} size="sm">
              <RotateCcw /> Retry
            </Button>
          </div>
        </>
      );
    case "done":
    case "failed":
      return (
        <>
          <TokenSummary summary={props.summary} />
          <div className="min-h-0 flex-1 overflow-auto">
            <EventFeed events={props.events} />
            <DiffView patch={props.patch} />
          </div>
          {props.node.status === "failed" ? (
            <div className="border-border border-t p-3">
              <Button disabled={busy} onClick={props.onRetry} size="sm">
                <RotateCcw /> Retry
              </Button>
            </div>
          ) : (
            <p className="border-border border-t p-3 text-meta text-fg-muted">
              Re-running an integrated node is not exposed by the current
              orchestrator contract.
            </p>
          )}
        </>
      );
    case "skipped":
      return (
        <p className="p-3 text-ui">
          This node was skipped because a dependency could not complete.
        </p>
      );
    case "pending":
    case "awaiting_review":
      return null;
  }
}
