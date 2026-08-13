import { GitBranch, Save, Trash2, X } from "lucide-react";
import { useState } from "react";
import type { Node, UpdateNode } from "@/api/client";
import { Button } from "@/components/ui/button";

type Props = {
  node: Node;
  busy: boolean;
  dependencies?: readonly string[];
  onClose?: () => void;
  onSave: (nodeId: string, update: UpdateNode) => void;
  onRemove: (nodeId: string) => void;
};

const CONTROL =
  "h-[30px] w-full border border-border-strong bg-inset px-2 text-ui text-fg outline-none focus:border-focus";

export function NodeEditor({
  node,
  busy,
  dependencies = [],
  onClose,
  onSave,
  onRemove,
}: Props) {
  const [name, setName] = useState(node.name);
  const [prompt, setPrompt] = useState(node.prompt);
  const [harness, setHarness] = useState(node.harness);
  const [model, setModel] = useState(node.model ?? "");
  const [criteria, setCriteria] = useState(node.acceptance_criteria.join("\n"));
  const [requiresReview, setRequiresReview] = useState(node.requires_review);

  function save() {
    onSave(node.id, {
      name: name.trim(),
      prompt: prompt.trim(),
      acceptance_criteria: criteria
        .split("\n")
        .map((criterion) => criterion.trim())
        .filter(Boolean),
      harness: harness.trim(),
      model: model.trim() || null,
      requires_review: requiresReview,
      touches: node.touches,
      estimated_effort: node.estimated_effort,
    });
  }

  return (
    <aside className="flex h-full max-h-full min-h-0 w-[480px] max-w-[60vw] shrink-0 flex-col overflow-hidden border-border border-l bg-surface shadow-2xl">
      <div className="flex h-7 items-center border-border border-b bg-inset/55 px-3 font-mono text-badge uppercase tracking-[0.12em] text-fg-subtle">
        Proposal inspector
      </div>
      <div className="flex items-start gap-2 border-border border-b px-3 py-3">
        <GitBranch className="mt-0.5 size-4 text-accent" />
        <div className="min-w-0 flex-1">
          <h2 className="font-semibold text-ui">Edit proposal node</h2>
          <code className="text-code text-fg-muted">{node.id}</code>
        </div>
        {onClose ? (
          <Button
            aria-label="Close node drawer"
            onClick={onClose}
            size="icon-sm"
            variant="ghost"
          >
            <X />
          </Button>
        ) : null}
      </div>
      <div className="min-h-0 flex-1 space-y-4 overflow-y-auto p-3">
        <label className="block text-meta text-fg-muted">
          Name
          <input
            aria-label="Node name"
            className={`${CONTROL} mt-1`}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
            value={name}
          />
        </label>
        <label className="block text-meta text-fg-muted">
          Prompt
          <textarea
            aria-label="Node prompt"
            className="mt-1 min-h-32 w-full resize-y border border-border-strong bg-inset p-2 text-ui text-fg outline-none focus:border-focus"
            disabled={busy}
            onChange={(event) => setPrompt(event.target.value)}
            value={prompt}
          />
        </label>
        <label className="block text-meta text-fg-muted">
          Harness
          <input
            aria-label="Node harness"
            className={`${CONTROL} mt-1 font-mono`}
            disabled={busy}
            list="agenthub-harnesses"
            onChange={(event) => setHarness(event.target.value)}
            value={harness}
          />
          <datalist id="agenthub-harnesses">
            <option value="claude-code" />
            <option value="codex" />
            <option value="opencode" />
          </datalist>
        </label>
        <label className="block text-meta text-fg-muted">
          Acceptance criteria, one per line
          <textarea
            aria-label="Node acceptance criteria"
            className="mt-1 min-h-24 w-full resize-y border border-border-strong bg-inset p-2 text-ui text-fg outline-none focus:border-focus"
            disabled={busy}
            onChange={(event) => setCriteria(event.target.value)}
            value={criteria}
          />
        </label>
        <div className="text-meta text-fg-muted">
          Dependencies
          <p className="mt-1 text-ui text-fg">
            {dependencies.length > 0 ? dependencies.join(", ") : "None"}
          </p>
        </div>
        <label className="block text-meta text-fg-muted">
          Model
          <input
            aria-label="Node model"
            className={`${CONTROL} mt-1 font-mono`}
            disabled={busy}
            onChange={(event) => setModel(event.target.value)}
            placeholder="Harness default"
            value={model}
          />
        </label>
        <label className="flex cursor-pointer items-start gap-3 border border-border-strong bg-inset p-3">
          <input
            aria-label="Require code review"
            checked={requiresReview}
            className="mt-0.5 size-4 shrink-0 accent-accent"
            disabled={busy}
            onChange={(event) => setRequiresReview(event.target.checked)}
            type="checkbox"
          />
          <span>
            <span className="block font-medium text-ui text-fg">
              Require code review
            </span>
            <span className="mt-0.5 block text-meta text-fg-muted">
              When enabled, pause after this node finishes for diff and
              acceptance review. When disabled, integrate successful changes
              automatically and release dependent nodes without approval.
            </span>
          </span>
        </label>
        <p className="text-meta text-fg-muted">
          Drag from a node's lower handle to another node's upper handle to add
          a dependency. Select an edge and press Delete to remove it.
        </p>
      </div>
      <div className="flex items-center justify-between gap-2 border-border border-t bg-elevated px-3 py-3">
        <Button
          disabled={busy || !name.trim() || !prompt.trim() || !harness.trim()}
          onClick={save}
          size="sm"
        >
          <Save /> Save
        </Button>
        <Button
          disabled={busy}
          onClick={() => onRemove(node.id)}
          size="sm"
          variant="destructive"
        >
          <Trash2 /> Remove node
        </Button>
      </div>
    </aside>
  );
}
