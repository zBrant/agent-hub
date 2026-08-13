import { Play, RotateCcw, Square } from "lucide-react";
import { useState } from "react";
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
  onRetry: (feedback?: string) => void;
};

function blockedReason(
  latest: Run | undefined,
  summary: RunSummary | null,
  events: readonly AgentEvent[],
  patch: string,
  dependencies: readonly string[],
) {
  if (!latest) {
    const names =
      dependencies.length > 0 ? ` (${dependencies.join(", ")})` : "";
    return `This node is blocked by an unfinished or blocked dependency${names}. Open that dependency first.`;
  }
  if (latest.status === "interrupted") {
    return "The previous process could not be safely adopted. Retry only after confirming it has stopped.";
  }
  if (summary?.trusted === false) {
    return "The harness output could not be parsed reliably, so AgentHub refused to merge it. Review the event feed and retry.";
  }
  if (
    latest.permission_denial_count > 0 ||
    events.some((event) => event.type === "permission")
  ) {
    return "The run encountered a permission gate. Review the event feed before retrying.";
  }
  if (latest.status === "success" && !patch.trim()) {
    return "The run finished successfully, but AgentHub found no repository changes to review. Add feedback below and retry.";
  }
  if (latest.status === "success" && patch.trim()) {
    return "The run finished successfully and its branch contains changes, but the checkpoint was not recognized. Retry now to recover the existing commit; the changes shown below will be preserved.";
  }
  return "The checkpoint could not be integrated, usually because of a merge conflict.";
}

function RetryPanel({
  busy,
  onRetry,
}: {
  busy: boolean;
  onRetry: (feedback?: string) => void;
}) {
  const [feedback, setFeedback] = useState("");
  const trimmed = feedback.trim();
  return (
    <div className="space-y-2 border-border border-t bg-surface p-3">
      <label className="block text-meta text-fg-muted">
        Retry feedback (optional)
        <textarea
          aria-label="Retry feedback"
          className="mt-1 min-h-20 w-full resize-y rounded-md border border-border-strong bg-inset p-2 text-ui text-fg"
          disabled={busy}
          onChange={(event) => setFeedback(event.target.value)}
          placeholder="Tell the next attempt what to change"
          value={feedback}
        />
      </label>
      <Button
        disabled={busy}
        onClick={() => onRetry(trimmed || undefined)}
        size="sm"
      >
        <RotateCcw /> Retry
      </Button>
    </div>
  );
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
        <h3 className="text-meta text-fg-muted">Code review</h3>
        <p className="mt-1 text-ui">
          {node.requires_review ? "Review required" : "Automatic integration"}
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
            {blockedReason(
              latest,
              props.summary,
              props.events,
              props.patch,
              props.dependencies,
            )}
          </p>
          <div className="min-h-0 flex-1">
            <DiffView patch={props.patch} />
          </div>
          {latest ? (
            <RetryPanel busy={busy} onRetry={props.onRetry} />
          ) : (
            <div className="border-border border-t p-3">
              <Button disabled={busy} onClick={props.onRun} size="sm">
                <Play /> Resume graph
              </Button>
            </div>
          )}
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
            <RetryPanel busy={busy} onRetry={props.onRetry} />
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
