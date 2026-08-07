import { type ClassValue, clsx } from "clsx";
import { extendTailwindMerge } from "tailwind-merge";

/**
 * Font-size names from the §3 scale and colour names from the §2 palette both
 * live in Tailwind's `text-*` namespace. Without this list tailwind-merge
 * cannot tell `text-ui` (a size) from `text-fg-muted` (a colour), puts them in
 * one group, and silently drops the first one.
 *
 * Keep in sync with `src/styles/tokens.css`. It is a merge hint, not a palette:
 * the values still live only in the token file.
 */
const TOKEN_TEXT_SIZES = [
  "title",
  "ui",
  "meta",
  "badge",
  "code",
  "term",
] as const;

const TOKEN_COLORS = [
  "bg",
  "surface",
  "elevated",
  "inset",
  "border",
  "border-strong",
  "fg",
  "fg-muted",
  "fg-subtle",
  "accent",
  "accent-hover",
  "accent-fg",
  "focus",
  "pending",
  "ready",
  "running",
  "review",
  "blocked",
  "done",
  "failed",
  "skipped",
  "harness-claude-code",
  "harness-codex",
  "harness-opencode",
] as const;

const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      text: [...TOKEN_TEXT_SIZES],
      color: [...TOKEN_COLORS],
      ease: ["panel"],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
