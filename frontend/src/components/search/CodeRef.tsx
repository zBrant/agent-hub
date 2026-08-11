import { ArrowUpRight, FileCode2 } from "lucide-react";
import type { AgentCitation } from "@/api/client";

type Props = {
  citation: AgentCitation;
  onOpen: (citation: AgentCitation) => void;
};

export function CodeRef({ citation, onOpen }: Props) {
  const range =
    citation.line === citation.end_line
      ? String(citation.line)
      : `${citation.line}-${citation.end_line}`;

  return (
    <button
      aria-label={`Open ${citation.path} lines ${range}`}
      className="group inline-flex h-6 max-w-full items-center gap-1.5 border-border border-b font-mono text-badge text-fg-muted hover:border-accent hover:text-fg"
      onClick={() => onOpen(citation)}
      type="button"
    >
      <FileCode2 aria-hidden="true" className="size-3 shrink-0 text-accent" />
      <span className="truncate">{citation.path}</span>
      <span className="shrink-0 text-fg-subtle">:{range}</span>
      <ArrowUpRight
        aria-hidden="true"
        className="size-3 shrink-0 text-fg-subtle group-hover:text-accent"
      />
    </button>
  );
}
