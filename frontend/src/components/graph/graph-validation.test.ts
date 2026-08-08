import { describe, expect, it } from "vitest";
import type { Node } from "@/api/client";
import { validateGraph } from "@/components/graph/graph-validation";

function node(id: string): Node {
  return {
    id,
    session_id: "sess_one",
    name: id,
    prompt: `Build ${id}`,
    acceptance_criteria: [],
    harness: "codex",
    model: "gpt-5.6-terra",
    touches: [],
    estimated_effort: null,
    worktree_path: null,
    branch: null,
    base_ref: null,
    status: "pending",
    created_ms: 1,
    updated_ms: 1,
  };
}

describe("client graph validation", () => {
  const nodes = [node("a"), node("b"), node("c")];

  it("accepts a valid DAG", () => {
    expect(
      validateGraph(nodes, [
        { depends_on_id: "a", node_id: "b" },
        { depends_on_id: "b", node_id: "c" },
      ]),
    ).toEqual({ valid: true, issues: [] });
  });

  it("rejects a cycle before approval", () => {
    const result = validateGraph(nodes, [
      { depends_on_id: "a", node_id: "b" },
      { depends_on_id: "b", node_id: "c" },
      { depends_on_id: "c", node_id: "a" },
    ]);

    expect(result.valid).toBe(false);
    expect(result.issues).toContain("The graph contains a dependency cycle.");
  });

  it("rejects self, duplicate, and orphan dependencies", () => {
    const result = validateGraph(nodes, [
      { depends_on_id: "a", node_id: "a" },
      { depends_on_id: "a", node_id: "b" },
      { depends_on_id: "a", node_id: "b" },
      { depends_on_id: "missing", node_id: "c" },
    ]);

    expect(result.valid).toBe(false);
    expect(result.issues).toEqual([
      "A node cannot depend on itself.",
      "The graph contains a duplicate dependency.",
      "A dependency points to a missing node.",
    ]);
  });
});
