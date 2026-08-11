import {
  Background,
  Controls,
  type Edge,
  MiniMap,
  type NodeTypes,
  ReactFlow,
  type XYPosition,
} from "@xyflow/react";
import { useEffect, useMemo, useState } from "react";
import type { Node } from "@/api/client";
import { type ActivityFlowNode, GraphNode } from "@/components/graph/GraphNode";
import { edgeId, type GraphEdge } from "@/components/graph/graph-validation";
import { cn } from "@/lib/utils";
import "@xyflow/react/dist/style.css";
import "@/styles/graph.css";

const NODE_TYPES = { activity: GraphNode } satisfies NodeTypes;

type Props = {
  nodes: readonly Node[];
  edges: readonly GraphEdge[];
  invalidEdgeIds: ReadonlySet<string>;
  editable: boolean;
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
  onConnect: (sourceId: string, targetId: string) => void;
  onDeleteNodes: (nodeIds: readonly string[]) => void;
  onDeleteEdges: (edgeIds: readonly string[]) => void;
};

async function layoutTopology(
  nodeIds: readonly string[],
  edges: readonly GraphEdge[],
): Promise<Record<string, XYPosition>> {
  const { default: ELK } = await import("elkjs/lib/elk.bundled.js");
  const elk = new ELK();
  const graph = await elk.layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "DOWN",
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.spacing.nodeNode": "32",
    },
    children: nodeIds.map((id) => ({ id, width: 240, height: 72 })),
    edges: edges.map((edge) => ({
      id: edgeId(edge),
      sources: [edge.depends_on_id],
      targets: [edge.node_id],
    })),
  });
  return Object.fromEntries(
    (graph.children ?? []).map((child) => [
      child.id,
      { x: child.x ?? 0, y: child.y ?? 0 },
    ]),
  );
}

export function GraphCanvas({
  nodes,
  edges,
  invalidEdgeIds,
  editable,
  selectedNodeId,
  onSelectNode,
  onConnect,
  onDeleteNodes,
  onDeleteEdges,
}: Props) {
  const [positions, setPositions] = useState<Record<string, XYPosition>>({});
  const nodeTopology = nodes
    .map((node) => node.id)
    .sort()
    .join("\n");
  const edgeTopology = edges.map(edgeId).sort().join("\n");
  const topology = useMemo(
    () => ({
      nodeIds: nodeTopology ? nodeTopology.split("\n") : [],
      edges: edgeTopology
        ? edgeTopology.split("\n").map((id) => {
            const [dependsOnId = "", nodeId = ""] = id.split("->");
            return { depends_on_id: dependsOnId, node_id: nodeId };
          })
        : [],
    }),
    [edgeTopology, nodeTopology],
  );

  useEffect(() => {
    let active = true;
    void layoutTopology(topology.nodeIds, topology.edges).then((next) => {
      if (active) setPositions(next);
    });
    return () => {
      active = false;
    };
  }, [topology]);

  const statusById = new Map(nodes.map((node) => [node.id, node.status]));
  const flowNodes: ActivityFlowNode[] = nodes.map((node, index) => ({
    id: node.id,
    type: "activity",
    ariaLabel: `${node.name}, ${node.status.replaceAll("_", " ")}`,
    position: positions[node.id] ?? { x: (index % 3) * 272, y: 0 },
    data: { editable, node },
    selected: selectedNodeId === node.id,
    deletable: editable,
    draggable: true,
  }));
  const flowEdges: Edge[] = edges.map((edge) => {
    const id = edgeId(edge);
    const connected =
      selectedNodeId === edge.node_id || selectedNodeId === edge.depends_on_id;
    return {
      id,
      source: edge.depends_on_id,
      target: edge.node_id,
      type: "smoothstep",
      deletable: editable,
      className: cn(
        "graph-edge",
        connected && "graph-edge-connected",
        invalidEdgeIds.has(id) && "graph-edge-invalid",
        statusById.get(edge.depends_on_id) !== "done" && "graph-edge-pending",
      ),
    };
  });

  return (
    <section
      aria-label="Activity graph"
      className="group h-full min-h-[420px] w-full bg-inset/30"
    >
      <ReactFlow<ActivityFlowNode, Edge>
        deleteKeyCode={editable ? ["Backspace", "Delete"] : null}
        edges={flowEdges}
        edgesReconnectable={false}
        elementsSelectable
        fitView
        fitViewOptions={{ padding: 0.16 }}
        nodeTypes={NODE_TYPES}
        nodes={flowNodes}
        nodesConnectable={editable}
        onConnect={(connection) => {
          if (connection.source && connection.target) {
            onConnect(connection.source, connection.target);
          }
        }}
        onEdgesDelete={(deleted) =>
          onDeleteEdges(deleted.map((edge) => edge.id))
        }
        onNodeClick={(_, node) => onSelectNode(node.id)}
        onNodesDelete={(deleted) =>
          onDeleteNodes(deleted.map((node) => node.id))
        }
        onPaneClick={() => onSelectNode(null)}
      >
        <Background className="graph-background" gap={28} size={1} />
        {nodes.length > 8 ? (
          <MiniMap
            className="graph-minimap! border! border-border! bg-inset!"
            maskColor="var(--color-graph-mask)"
            nodeColor="var(--color-border-strong)"
          />
        ) : null}
        <Controls className="graph-controls!" showInteractive={false} />
      </ReactFlow>
    </section>
  );
}
