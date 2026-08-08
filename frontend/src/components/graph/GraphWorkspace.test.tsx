// @vitest-environment jsdom

import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Graph, Node } from "@/api/client";
import { GraphWorkspace } from "@/components/graph/GraphWorkspace";

const canvas = vi.hoisted(() => ({
  props: null as null | {
    onConnect: (sourceId: string, targetId: string) => void;
    onDeleteEdges: (edgeIds: readonly string[]) => void;
    onDeleteNodes: (nodeIds: readonly string[]) => void;
    onSelectNode: (nodeId: string | null) => void;
  },
}));

vi.mock("@/components/graph/GraphCanvas", () => ({
  GraphCanvas: (props: NonNullable<typeof canvas.props>) => {
    canvas.props = props;
    return <div>Canvas</div>;
  },
}));

const session = {
  id: "sess_one",
  title: "Build the graph",
  repo_path: "/repo",
  workspace_root: "/workspace",
  integration_branch: "agenthub/sess_one/integration",
  auto_merge: false,
  status: "planning",
  created_ms: 1,
  updated_ms: 1,
} as const;

function node(id: string, name: string): Node {
  return {
    id,
    session_id: session.id,
    name,
    prompt: `Build ${name}`,
    acceptance_criteria: ["Tests pass"],
    harness: "codex",
    model: "gpt-5.6-terra",
    touches: ["frontend/**"],
    estimated_effort: "medium",
    worktree_path: null,
    branch: null,
    base_ref: null,
    status: "pending",
    created_ms: 1,
    updated_ms: 1,
  };
}

function graph(firstName = "First"): Graph {
  return {
    session,
    nodes: [node("node_a", firstName), node("node_b", "Second")],
    edges: [
      {
        session_id: session.id,
        depends_on_id: "node_a",
        node_id: "node_b",
        created_ms: 1,
      },
    ],
  };
}

function wrapper({ children }: { children: ReactNode }) {
  return <MemoryRouter>{children}</MemoryRouter>;
}

function actions() {
  return {
    onUpdateNode: vi.fn().mockResolvedValue(undefined),
    onDeleteNode: vi.fn().mockResolvedValue(undefined),
    onAddDependency: vi.fn().mockResolvedValue(undefined),
    onRemoveDependency: vi.fn().mockResolvedValue(undefined),
    onApprove: vi.fn().mockResolvedValue(undefined),
  };
}

describe("editable graph workspace", () => {
  afterEach(() => {
    cleanup();
    canvas.props = null;
  });

  it("keeps approval disabled while a client draft contains a cycle", () => {
    const callbacks = actions();
    render(<GraphWorkspace graph={graph()} {...callbacks} />, { wrapper });

    const approve = screen.getByRole("button", { name: "Approve graph" });
    expect((approve as HTMLButtonElement).disabled).toBe(false);

    act(() => canvas.props?.onConnect("node_b", "node_a"));

    expect(screen.getByText("Invalid graph")).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toContain("cycle");
    expect((approve as HTMLButtonElement).disabled).toBe(true);
    expect(callbacks.onAddDependency).not.toHaveBeenCalled();

    act(() => canvas.props?.onDeleteEdges(["node_b->node_a"]));
    expect(screen.queryByText("Invalid graph")).toBeNull();
    expect((approve as HTMLButtonElement).disabled).toBe(false);
  });

  it("opens a node drawer from a dashboard deep link", () => {
    render(
      <GraphWorkspace
        graph={graph()}
        initialSelectedNodeId="node_b"
        renderNodeDrawer={(selected) => <aside>{selected.name} drawer</aside>}
        {...actions()}
      />,
      { wrapper },
    );

    expect(screen.getByText("Second drawer")).toBeTruthy();
  });

  it("persists a complete node replacement and reloads the saved values", async () => {
    const callbacks = actions();
    const first = render(<GraphWorkspace graph={graph()} {...callbacks} />, {
      wrapper,
    });
    act(() => canvas.props?.onSelectNode("node_a"));

    fireEvent.change(screen.getByLabelText("Node name"), {
      target: { value: "Renamed" },
    });
    fireEvent.change(screen.getByLabelText("Node prompt"), {
      target: { value: "Build the renamed node" },
    });
    fireEvent.change(screen.getByLabelText("Node harness"), {
      target: { value: "claude-code" },
    });
    fireEvent.change(screen.getByLabelText("Node model"), {
      target: { value: "claude-opus-5" },
    });
    fireEvent.change(screen.getByLabelText("Node acceptance criteria"), {
      target: { value: "Tests pass\nDocs updated" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(callbacks.onUpdateNode).toHaveBeenCalledWith("node_a", {
        name: "Renamed",
        prompt: "Build the renamed node",
        acceptance_criteria: ["Tests pass", "Docs updated"],
        harness: "claude-code",
        model: "claude-opus-5",
        touches: ["frontend/**"],
        estimated_effort: "medium",
      }),
    );

    first.unmount();
    const reloaded = graph("Renamed");
    const saved = {
      ...reloaded,
      nodes: reloaded.nodes.map((item) =>
        item.id === "node_a"
          ? {
              ...item,
              prompt: "Build the renamed node",
              acceptance_criteria: ["Tests pass", "Docs updated"],
            }
          : item,
      ),
    };
    render(<GraphWorkspace graph={saved} {...actions()} />, {
      wrapper,
    });
    act(() => canvas.props?.onSelectNode("node_a"));
    expect((screen.getByLabelText("Node name") as HTMLInputElement).value).toBe(
      "Renamed",
    );
    expect(
      (screen.getByLabelText("Node prompt") as HTMLTextAreaElement).value,
    ).toBe("Build the renamed node");
    expect(
      (screen.getByLabelText("Node acceptance criteria") as HTMLTextAreaElement)
        .value,
    ).toBe("Tests pass\nDocs updated");
  });

  it("refuses a non-atomic multi-node removal", () => {
    const callbacks = actions();
    render(<GraphWorkspace graph={graph()} {...callbacks} />, { wrapper });

    act(() => canvas.props?.onDeleteNodes(["node_a", "node_b"]));

    expect(screen.getByRole("alert").textContent).toContain(
      "one node at a time",
    );
    expect(callbacks.onDeleteNode).not.toHaveBeenCalled();
  });

  it("opens a drawer when a running graph node is selected", () => {
    const callbacks = actions();
    const running = {
      ...graph(),
      nodes: graph().nodes.map((item) => ({
        ...item,
        status: "running" as const,
      })),
    };
    render(
      <GraphWorkspace
        graph={running}
        {...callbacks}
        renderNodeDrawer={(selected) => (
          <aside>Live drawer for {selected.name}</aside>
        )}
      />,
      { wrapper },
    );

    act(() => canvas.props?.onSelectNode("node_b"));

    expect(screen.getByText("Live drawer for Second")).toBeTruthy();
  });
});
