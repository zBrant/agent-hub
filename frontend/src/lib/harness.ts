/**
 * Harness identity colours from docs/design-system.md §6, encoded once.
 *
 * Harness colour is identity, never state: the palette is deliberately kept
 * away from the §5 state tokens. Every consumer takes the class string from
 * here rather than re-deciding which harness is which colour.
 */
export function harnessDotClass(harness: string | null): string {
  switch (harness) {
    case "claude-code":
      return "bg-harness-claude-code";
    case "codex":
      return "bg-harness-codex";
    case "opencode":
      return "bg-harness-opencode";
    default:
      return "bg-fg-subtle";
  }
}
