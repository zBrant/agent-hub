import type { Node, NodeDependency } from "@/api/client";

export type GraphEdge = Pick<NodeDependency, "depends_on_id" | "node_id">;

export type GraphValidation = {
  valid: boolean;
  issues: readonly string[];
};

export function edgeId(edge: GraphEdge): string {
  return `${edge.depends_on_id}->${edge.node_id}`;
}

/** Validate the whole client draft before it reaches the approval gate. */
export function validateGraph(
  nodes: readonly Node[],
  edges: readonly GraphEdge[],
): GraphValidation {
  const issues: string[] = [];
  if (nodes.length === 0) {
    return { valid: false, issues: ["A graph needs at least one node."] };
  }

  const nodeIds = new Set(nodes.map((node) => node.id));
  const seenEdges = new Set<string>();
  const indegree = new Map(nodes.map((node) => [node.id, 0]));
  const children = new Map(nodes.map((node) => [node.id, [] as string[]]));

  for (const edge of edges) {
    const key = edgeId(edge);
    if (seenEdges.has(key)) {
      issues.push("The graph contains a duplicate dependency.");
      continue;
    }
    seenEdges.add(key);
    if (edge.node_id === edge.depends_on_id) {
      issues.push("A node cannot depend on itself.");
      continue;
    }
    if (!nodeIds.has(edge.node_id) || !nodeIds.has(edge.depends_on_id)) {
      issues.push("A dependency points to a missing node.");
      continue;
    }
    indegree.set(edge.node_id, (indegree.get(edge.node_id) ?? 0) + 1);
    children.get(edge.depends_on_id)?.push(edge.node_id);
  }

  if (issues.length === 0) {
    const ready = nodes
      .filter((node) => indegree.get(node.id) === 0)
      .map((node) => node.id);
    let visited = 0;
    while (ready.length > 0) {
      const current = ready.shift();
      if (!current) break;
      visited += 1;
      for (const child of children.get(current) ?? []) {
        const remaining = (indegree.get(child) ?? 0) - 1;
        indegree.set(child, remaining);
        if (remaining === 0) ready.push(child);
      }
    }
    if (visited !== nodes.length) {
      issues.push("The graph contains a dependency cycle.");
    }
  }

  return { valid: issues.length === 0, issues };
}
