import { FileCode2 } from "lucide-react";
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
      className="inline-flex h-6 max-w-full items-center gap-1 rounded-sm border border-border bg-inset px-1.5 font-mono text-badge text-accent hover:border-border-strong hover:text-accent-hover"
      onClick={() => onOpen(citation)}
      type="button"
    >
      <FileCode2 aria-hidden="true" className="size-3 shrink-0" />
      <span className="truncate">{citation.path}</span>
      <span className="shrink-0 text-fg-muted">:{range}</span>
    </button>
  );
}
