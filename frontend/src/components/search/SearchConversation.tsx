import { AlertTriangle, Bot, Search, UserRound } from "lucide-react";
import type { FormEvent, KeyboardEvent } from "react";
import type { AgentCitation, AgentEvidence, AgentSearch } from "@/api/client";
import { CodeRef } from "@/components/search/CodeRef";
import { Button } from "@/components/ui/button";

export type SearchTurn = {
  id: number;
  question: string;
  answer?: AgentSearch;
  error?: string;
};

type Props = {
  turns: readonly SearchTurn[];
  question: string;
  pending: boolean;
  disabled: boolean;
  onQuestionChange: (question: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onOpenCitation: (citation: AgentCitation) => void;
  onOpenEvidence: (evidence: AgentEvidence) => void;
};

export function SearchConversation({
  turns,
  question,
  pending,
  disabled,
  onQuestionChange,
  onSubmit,
  onOpenCitation,
  onOpenEvidence,
}: Props) {
  function submitShortcut(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || (!event.metaKey && !event.ctrlKey)) return;
    event.preventDefault();
    event.currentTarget.form?.requestSubmit();
  }

  return (
    <section className="flex h-full min-h-0 flex-col bg-bg">
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {turns.length ? (
          <div className="mx-auto grid max-w-3xl gap-4">
            {turns.map((turn) => (
              <SearchExchange
                key={turn.id}
                turn={turn}
                onOpenCitation={onOpenCitation}
                onOpenEvidence={onOpenEvidence}
              />
            ))}
          </div>
        ) : (
          <div className="flex h-full min-h-64 items-center justify-center">
            <div className="max-w-md text-center">
              <Search className="mx-auto mb-3 size-6 text-accent" />
              <h2 className="font-semibold text-ui">Ask about the codebase</h2>
              <p className="mt-1 text-fg-muted text-meta">
                The search agent navigates text, syntax and symbols, then links
                every supported claim to lines it read.
              </p>
            </div>
          </div>
        )}
      </div>

      <form
        className="border-border border-t bg-surface p-3"
        onSubmit={onSubmit}
      >
        <div className="mx-auto flex max-w-3xl items-end gap-2">
          <label className="sr-only" htmlFor="search-question">
            Repository question
          </label>
          <textarea
            className="min-h-16 flex-1 resize-y rounded-md border border-border bg-inset px-2 py-1.5 text-fg text-ui outline-none placeholder:text-fg-subtle focus:border-focus disabled:opacity-60"
            disabled={disabled || pending}
            id="search-question"
            onChange={(event) => onQuestionChange(event.target.value)}
            onKeyDown={submitShortcut}
            placeholder="Where is the recurring discount rule enforced?"
            value={question}
          />
          <Button
            disabled={disabled || pending || !question.trim()}
            type="submit"
          >
            {pending ? "Searching…" : "Ask"}
          </Button>
        </div>
        <p className="mx-auto mt-1 max-w-3xl text-badge text-fg-subtle">
          Press ⌘↵ or Ctrl↵ to send. Search is read-only.
        </p>
      </form>
    </section>
  );
}

function SearchExchange({
  turn,
  onOpenCitation,
  onOpenEvidence,
}: {
  turn: SearchTurn;
  onOpenCitation: (citation: AgentCitation) => void;
  onOpenEvidence: (evidence: AgentEvidence) => void;
}) {
  const supportedClaims = turn.answer?.claims.filter(
    (claim) => claim.citations.length > 0,
  );

  return (
    <article className="grid gap-2">
      <div className="flex gap-2 rounded-lg border border-border bg-surface p-3">
        <UserRound className="mt-0.5 size-4 shrink-0 text-fg-muted" />
        <p className="whitespace-pre-wrap leading-relaxed">{turn.question}</p>
      </div>
      <div className="flex gap-2 px-3 py-2">
        <Bot className="mt-0.5 size-4 shrink-0 text-accent" />
        {turn.error ? (
          <p className="text-failed text-meta" role="alert">
            {turn.error}
          </p>
        ) : turn.answer ? (
          <div className="min-w-0 flex-1">
            {turn.answer.complete && !supportedClaims?.length ? (
              <p className="text-failed text-meta">
                The response contained no linked claims and was not rendered.
              </p>
            ) : (
              <ol className="grid gap-3">
                {supportedClaims?.map((claim) => (
                  <li key={`${turn.id}:${claim.text}`}>
                    <p className="leading-relaxed">{claim.text}</p>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {claim.citations.map((citation) => (
                        <CodeRef
                          citation={citation}
                          key={`${citation.path}:${citation.line}:${citation.end_line}`}
                          onOpen={onOpenCitation}
                        />
                      ))}
                    </div>
                  </li>
                ))}
              </ol>
            )}
            {!turn.answer.complete ? (
              <div className="mt-2 rounded-md border border-review bg-inset p-2">
                <p className="flex items-center gap-1 text-review text-meta">
                  <AlertTriangle className="size-3.5" />
                  {turn.answer.message}
                </p>
                {turn.answer.evidence.length ? (
                  <div className="mt-1 flex flex-wrap gap-1">
                    {turn.answer.evidence.map((evidence) => (
                      <button
                        className="font-mono text-badge text-fg-muted hover:text-fg"
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
            <p className="mt-2 text-badge text-fg-subtle">
              {turn.answer.turns} turns · {turn.answer.tool_calls} tools ·{" "}
              {turn.answer.usage.total_tokens.toLocaleString("en")} tokens
            </p>
          </div>
        ) : (
          <p aria-live="polite" className="text-fg-muted text-meta">
            Navigating the repository and validating citations…
          </p>
        )}
      </div>
    </article>
  );
}
