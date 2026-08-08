import { Save, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Node, UpdateNode } from "@/api/client";
import { Button } from "@/components/ui/button";

type Props = {
  node: Node;
  busy: boolean;
  onSave: (nodeId: string, update: UpdateNode) => void;
  onRemove: (nodeId: string) => void;
};

const CONTROL =
  "h-[30px] w-full rounded-md border border-border-strong bg-inset px-2 text-ui text-fg outline-none focus:border-focus";

export function NodeEditor({ node, busy, onSave, onRemove }: Props) {
  const [name, setName] = useState(node.name);
  const [harness, setHarness] = useState(node.harness);
  const [model, setModel] = useState(node.model ?? "");

  function save() {
    onSave(node.id, {
      name: name.trim(),
      prompt: node.prompt,
      acceptance_criteria: node.acceptance_criteria,
      harness: harness.trim(),
      model: model.trim() || null,
      touches: node.touches,
      estimated_effort: node.estimated_effort,
    });
  }

  return (
    <aside className="w-[340px] shrink-0 border-border border-l bg-surface p-3">
      <div className="mb-3">
        <h2 className="font-semibold text-ui">Edit proposal node</h2>
        <code className="text-code text-fg-muted">{node.id}</code>
      </div>
      <div className="space-y-3">
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
        <p className="text-meta text-fg-muted">
          Drag from a node's lower handle to another node's upper handle to add
          a dependency. Select an edge and press Delete to remove it.
        </p>
        <div className="flex items-center justify-between gap-2 border-border border-t pt-3">
          <Button
            disabled={busy || !name.trim() || !harness.trim()}
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
      </div>
    </aside>
  );
}
