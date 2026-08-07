import {
  AlertTriangle,
  Check,
  Circle,
  CircleDot,
  Eye,
  Loader,
  type LucideIcon,
  SkipForward,
  X,
} from "lucide-react";

/**
 * The node-state vocabulary of docs/design-system.md §5 — colour, icon and
 * label — encoded exactly once.
 *
 * Never redefine a state colour in a component: design-system §2 ("a `#` in a
 * component is a bug") and §1.3 ("state is never colour alone"). Every consumer
 * takes the class strings and the icon from here.
 *
 * This is presentation, not a mirror of a backend model: it maps the design
 * system's vocabulary. When `src/api/schema.d.ts` exists, add a compile-time
 * assertion that the backend's node status enum is exactly `NodeState` — the
 * two vocabularies have to line up, and a type error is the cheapest way to
 * find out that they stopped.
 */
export const NODE_STATES = [
  "pending",
  "ready",
  "running",
  "awaiting_review",
  "blocked",
  "done",
  "failed",
  "skipped",
] as const;

export type NodeState = (typeof NODE_STATES)[number];

export type NodeStateVisual = {
  /** Written label — also the `aria-label` text (design-system §11). */
  label: string;
  icon: LucideIcon;
  /** Foreground utility for the icon and any state text. */
  text: string;
  /** 1.5px border colour of the graph node (design-system §5). */
  border: string;
  /** Tinted fill for badges and dots. */
  fill: string;
};

/**
 * The class strings are written out literally so Tailwind's scanner sees them.
 * Building them as `text-${state}` would produce no CSS.
 */
export const NODE_STATE_VISUAL: Record<NodeState, NodeStateVisual> = {
  pending: {
    label: "Pending",
    icon: Circle,
    text: "text-pending",
    border: "border-pending",
    fill: "bg-pending",
  },
  ready: {
    label: "Ready",
    icon: CircleDot,
    text: "text-ready",
    border: "border-ready",
    fill: "bg-ready",
  },
  running: {
    label: "Running",
    icon: Loader,
    text: "text-running",
    border: "border-running",
    fill: "bg-running",
  },
  awaiting_review: {
    label: "Awaiting review",
    icon: Eye,
    text: "text-review",
    border: "border-review",
    fill: "bg-review",
  },
  blocked: {
    label: "Blocked",
    icon: AlertTriangle,
    text: "text-blocked",
    border: "border-blocked",
    fill: "bg-blocked",
  },
  done: {
    label: "Done",
    icon: Check,
    text: "text-done",
    border: "border-done",
    fill: "bg-done",
  },
  failed: {
    label: "Failed",
    icon: X,
    text: "text-failed",
    border: "border-failed",
    fill: "bg-failed",
  },
  skipped: {
    label: "Skipped",
    icon: SkipForward,
    text: "text-skipped",
    border: "border-skipped",
    fill: "bg-skipped",
  },
};

export function nodeStateVisual(state: NodeState): NodeStateVisual {
  return NODE_STATE_VISUAL[state];
}
