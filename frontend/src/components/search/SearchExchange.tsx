import { AlertTriangle, CheckCircle2, CircleDashed } from "lucide-react";
import type { AgentCitation, AgentEvidence, AgentSearch } from "@/api/client";
import { CodeRef } from "@/components/search/CodeRef";

export type SearchTurn = {
  id: number;
  question: string;
  answer?: AgentSearch;
  error?: string;
};

type Props = {
  turn: SearchTurn;
  index: number;
  onOpenCitation: (citation: AgentCitation) => void;
  onOpenEvidence: (evidence: AgentEvidence) => void;
};

export function SearchExchange({
  turn,
  index,
  onOpenCitation,
  onOpenEvidence,
}: Props) {
  const supportedClaims = turn.answer?.claims.filter(
    (claim) => claim.citations.length > 0,
  );
  const citationCount = supportedClaims?.reduce(
    (total, claim) => total + claim.citations.length,
    0,
  );

  return (
    <article
      className="scroll-mt-2 border-border border-b"
      id={`investigation-${turn.id}`}
    >
      <header className="flex gap-3 bg-surface px-4 py-3">
        <span className="mt-0.5 font-mono text-badge text-accent">
          Q{String(index).padStart(2, "0")}
        </span>
        <p className="whitespace-pre-wrap font-medium leading-relaxed">
          {turn.question}
        </p>
      </header>
      <div className="px-4 py-4">
        {turn.error ? (
          <div
            className="flex gap-2 border border-failed bg-surface p-3 text-failed text-meta"
            role="alert"
          >
            <AlertTriangle className="size-3.5 shrink-0" />
            {turn.error}
          </div>
        ) : turn.answer ? (
          <div className="min-w-0">
            <div className="mb-3 flex items-center justify-between">
              <p className="font-medium text-badge text-fg-subtle uppercase tracking-[0.1em]">
                Findings
              </p>
              <span className="flex items-center gap-1.5 text-badge text-fg-muted">
                <CheckCircle2 className="size-3 text-done" />
                {supportedClaims?.length ?? 0} claims · {citationCount ?? 0}{" "}
                sources
              </span>
            </div>
            {turn.answer.complete && !supportedClaims?.length ? (
              <p className="text-failed text-meta">
                The response contained no linked claims and was not rendered.
              </p>
            ) : (
              <ol className="grid gap-4">
                {supportedClaims?.map((claim, claimIndex) => (
                  <li
                    className="grid grid-cols-[24px_minmax(0,1fr)] gap-2"
                    key={`${turn.id}:${claim.text}`}
                  >
                    <span className="grid size-5 place-items-center border border-border font-mono text-badge text-fg-subtle">
                      {claimIndex + 1}
                    </span>
                    <div className="min-w-0">
                      <p className="leading-relaxed">{claim.text}</p>
                      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                        {claim.citations.map((citation) => (
                          <CodeRef
                            citation={citation}
                            key={`${citation.path}:${citation.line}:${citation.end_line}`}
                            onOpen={onOpenCitation}
                          />
                        ))}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            )}
            {!turn.answer.complete ? (
              <div className="mt-4 border border-review bg-surface p-3">
                <p className="flex items-center gap-1.5 text-review text-meta">
                  <AlertTriangle className="size-3.5 shrink-0" />
                  {turn.answer.message}
                </p>
                {turn.answer.evidence.length ? (
                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
                    {turn.answer.evidence.map((evidence) => (
                      <button
                        className="border-border border-b font-mono text-badge text-fg-muted hover:border-accent hover:text-fg"
                        key={`${evidence.path}:${evidence.line}:${evidence.end_line}`}
                        onClick={() => onOpenEvidence(evidence)}
                        type="button"
                      >
                        {evidence.path}:{evidence.line}-{evidence.end_line}
                      </button>
                    ))}
                  </div>
                ) : null}
              </div>
            ) : null}
            <p className="mt-4 border-border border-t pt-2 font-mono text-badge text-fg-subtle">
              {turn.answer.turns} reasoning turns · {turn.answer.tool_calls}{" "}
              tool operations · {formatBytes(turn.answer.bytes_read)} read ·{" "}
              {turn.answer.usage.total_tokens.toLocaleString("en")} tokens
            </p>
          </div>
        ) : (
          <p
            aria-live="polite"
            className="flex items-center gap-2 text-fg-muted text-meta"
          >
            <CircleDashed className="size-3.5 text-running" />
            Navigating the repository and validating citations…
          </p>
        )}
      </div>
    </article>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}
