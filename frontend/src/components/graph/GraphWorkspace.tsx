import {
  AlertTriangle,
  ArrowLeft,
  Check,
  FileCode2,
  Loader,
  X,
} from "lucide-react";
import type { ReactNode } from "react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import type { Graph, UpdateNode } from "@/api/client";
import { GraphCanvas } from "@/components/graph/GraphCanvas";
import {
  edgeId,
  type GraphEdge,
  validateGraph,
} from "@/components/graph/graph-validation";
import { NodeEditor } from "@/components/graph/NodeEditor";
import { DiffView } from "@/components/session/DiffView";
import { Button } from "@/components/ui/button";

type Props = {
  graph: Graph;
  onUpdateNode: (nodeId: string, update: UpdateNode) => Promise<void>;
  onDeleteNode: (nodeId: string) => Promise<void>;
  onAddDependency: (nodeId: string, dependsOnId: string) => Promise<void>;
  onRemoveDependency: (nodeId: string, dependsOnId: string) => Promise<void>;
  onApprove: () => Promise<void>;
  resultPatch?: string | null;
  resultBranch?: string | null;
  resultLoading?: boolean;
  resultError?: string | null;
  renderNodeDrawer?: (
    node: Graph["nodes"][number],
    onClose: () => void,
  ) => ReactNode;
  initialSelectedNodeId?: string | null;
};

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Unexpected local API error";
}

export function GraphWorkspace({
  graph,
  onUpdateNode,
  onDeleteNode,
  onAddDependency,
  onRemoveDependency,
  onApprove,
  resultPatch = null,
  resultBranch = null,
  resultLoading = false,
  resultError = null,
  renderNodeDrawer,
  initialSelectedNodeId = null,
}: Props) {
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(
    initialSelectedNodeId,
  );
  const [draftEdges, setDraftEdges] = useState<readonly GraphEdge[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showResult, setShowResult] = useState(false);
  const edges = useMemo(
    () => [...graph.edges, ...draftEdges],
    [draftEdges, graph.edges],
  );
  const validation = useMemo(
    () => validateGraph(graph.nodes, edges),
    [edges, graph.nodes],
  );
  const invalidEdgeIds = useMemo(
    () => new Set(draftEdges.map(edgeId)),
    [draftEdges],
  );
  const selectedNode = graph.nodes.find((node) => node.id === selectedNodeId);
  const editable = graph.nodes.every((node) => node.status === "pending");
  const complete =
    graph.nodes.length > 0 &&
    graph.nodes.every((node) => ["done", "skipped"].includes(node.status));
  const runningCount = graph.nodes.filter(
    (node) => node.status === "running",
  ).length;
  const reviewCount = graph.nodes.filter(
    (node) => node.status === "awaiting_review",
  ).length;
  const doneCount = graph.nodes.filter((node) => node.status === "done").length;

  async function perform(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  }

  function connect(sourceId: string, targetId: string) {
    const candidate = { depends_on_id: sourceId, node_id: targetId };
    if (edges.some((edge) => edgeId(edge) === edgeId(candidate))) return;
    const next = [...edges, candidate];
    if (!validateGraph(graph.nodes, next).valid) {
      setDraftEdges((current) => [...current, candidate]);
      return;
    }
    void perform(() => onAddDependency(targetId, sourceId));
  }

  function deleteEdges(edgeIds: readonly string[]) {
    const idSet = new Set(edgeIds);
    const persisted = graph.edges.filter((edge) => idSet.has(edgeId(edge)));
    setDraftEdges((current) =>
      current.filter((edge) => !idSet.has(edgeId(edge))),
    );
    if (persisted.length === 0) return;
    void perform(async () => {
      for (const edge of persisted) {
        await onRemoveDependency(edge.node_id, edge.depends_on_id);
      }
    });
  }

  function deleteNodes(nodeIds: readonly string[]) {
    if (nodeIds.length === 0) return;
    if (nodeIds.length > 1) {
      setError("Remove one node at a time so every edit remains atomic.");
      return;
    }
    void perform(async () => {
      for (const nodeId of nodeIds) await onDeleteNode(nodeId);
      setSelectedNodeId(null);
    });
  }

  return (
    <div className="flex min-h-full flex-col bg-bg">
      <header className="border-border border-b bg-surface">
        <div className="flex min-h-14 items-center gap-3 px-4 py-2">
          <Link
            aria-label="All sessions"
            className="flex size-7 items-center justify-center border border-border bg-inset text-fg-muted hover:text-fg"
            to="/sessions"
          >
            <ArrowLeft className="size-4" />
          </Link>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h1 className="truncate font-semibold text-title">
                {graph.session.title}
              </h1>
              <span className="hidden font-mono text-badge text-fg-subtle sm:inline">
                {graph.session.id}
              </span>
            </div>
            <p className="text-meta text-fg-muted">
              {editable
                ? "Proposal workspace · execution locked"
                : "Execution workspace · proposal locked"}
            </p>
          </div>
          {!validation.valid ? (
            <span className="inline-flex items-center gap-1 text-meta text-failed">
              <AlertTriangle className="size-3" /> Invalid graph
            </span>
          ) : (
            <span className="hidden items-center gap-1 text-meta text-done sm:inline-flex">
              <Check className="size-3" /> Valid DAG
            </span>
          )}
          <Button
            disabled={!editable || !validation.valid || busy}
            onClick={() => void perform(onApprove)}
            size="sm"
          >
            {busy ? (
              <Loader className="animate-spin" data-motion="essential" />
            ) : (
              <Check />
            )}
            Approve graph
          </Button>
          {complete ? (
            <Button
              onClick={() => {
                setSelectedNodeId(null);
                setShowResult(true);
              }}
              size="sm"
              variant="secondary"
            >
              <FileCode2 /> View generated code
            </Button>
          ) : null}
        </div>
        <div className="flex h-8 items-center gap-5 overflow-x-auto border-border border-t bg-bg/45 px-4 text-badge text-fg-muted">
          <span>
            <strong className="font-mono font-medium text-fg">
              {graph.nodes.length}
            </strong>{" "}
            nodes
          </span>
          <span>
            <strong className="font-mono font-medium text-fg">
              {graph.edges.length}
            </strong>{" "}
            dependencies
          </span>
          <span>
            <strong className="font-mono font-medium text-running">
              {runningCount}
            </strong>{" "}
            running
          </span>
          <span>
            <strong className="font-mono font-medium text-review">
              {reviewCount}
            </strong>{" "}
            review
          </span>
          <span>
            <strong className="font-mono font-medium text-done">
              {doneCount}
            </strong>{" "}
            integrated
          </span>
          <span className="ml-auto hidden text-fg-subtle md:inline">
            Select a node to inspect its worktree
          </span>
        </div>
      </header>
      {(error ?? validation.issues[0]) ? (
        <div
          className="border-failed border-b bg-failed/10 px-4 py-2 text-meta text-failed"
          role="alert"
        >
          {error ?? validation.issues[0]}
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1">
        <main className="min-w-0 flex-1">
          <GraphCanvas
            edges={edges}
            editable={editable && !busy}
            invalidEdgeIds={invalidEdgeIds}
            nodes={graph.nodes}
            onConnect={connect}
            onDeleteEdges={deleteEdges}
            onDeleteNodes={deleteNodes}
            onSelectNode={(nodeId) => {
              setShowResult(false);
              setSelectedNodeId(nodeId);
            }}
            selectedNodeId={selectedNodeId}
          />
        </main>
        {selectedNode
          ? (renderNodeDrawer?.(selectedNode, () => setSelectedNodeId(null)) ??
            (editable ? (
              <NodeEditor
                key={selectedNode.id}
                busy={busy}
                node={selectedNode}
                onRemove={(nodeId) => deleteNodes([nodeId])}
                onSave={(nodeId, update) =>
                  void perform(() => onUpdateNode(nodeId, update))
                }
              />
            ) : null))
          : null}
        {showResult && complete ? (
          <aside className="flex w-[560px] max-w-[65vw] shrink-0 flex-col border-border border-l bg-elevated shadow-2xl">
            <header className="flex items-start gap-2 border-border border-b px-3 py-2">
              <FileCode2 className="mt-0.5 size-4 text-done" />
              <div className="min-w-0 flex-1">
                <h2 className="font-semibold text-ui">Generated result</h2>
                <p className="text-meta text-fg-muted">
                  The original checkout is unchanged until you explicitly merge
                  this branch.
                </p>
              </div>
              <Button
                aria-label="Close generated result"
                onClick={() => setShowResult(false)}
                size="icon-sm"
                variant="ghost"
              >
                <X />
              </Button>
            </header>
            <div className="space-y-2 border-border border-b p-3 text-meta">
              <div>
                <span className="text-fg-muted">Result branch</span>
                <code className="mt-1 block break-all text-code">
                  {resultBranch ?? graph.session.final_branch}
                </code>
              </div>
              <div>
                <span className="text-fg-muted">Original checkout</span>
                <code className="mt-1 block break-all text-code">
                  {graph.session.repo_path}
                </code>
              </div>
            </div>
            {resultError ? (
              <p className="border-failed border-b bg-failed/10 p-3 text-meta text-failed">
                {resultError}
              </p>
            ) : null}
            <div className="min-h-0 flex-1">
              {resultLoading ? (
                <div className="flex h-full items-center justify-center gap-2 text-meta text-fg-muted">
                  <Loader
                    className="size-4 animate-spin"
                    data-motion="essential"
                  />
                  Loading generated diff…
                </div>
              ) : (
                <DiffView patch={resultPatch ?? ""} />
              )}
            </div>
          </aside>
        ) : null}
      </div>
    </div>
  );
}
