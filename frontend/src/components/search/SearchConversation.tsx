import { ArrowUp, BookOpenCheck, LockKeyhole } from "lucide-react";
import type { FormEvent, KeyboardEvent } from "react";
import type { AgentCitation, AgentEvidence } from "@/api/client";
import { EmptyInvestigation } from "@/components/search/EmptyInvestigation";
import { InquiryIndex } from "@/components/search/InquiryIndex";
import {
  SearchExchange,
  type SearchTurn,
} from "@/components/search/SearchExchange";
import { Button } from "@/components/ui/button";

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
      <div className="flex min-h-0 flex-1">
        {turns.length ? <InquiryIndex turns={turns} /> : null}
        <div className="min-h-0 min-w-0 flex-1 overflow-y-auto">
          {turns.length ? (
            <div className="mx-auto max-w-3xl">
              <header className="flex items-center justify-between border-border border-b px-4 py-3">
                <div>
                  <p className="font-medium text-badge text-fg-subtle uppercase tracking-[0.12em]">
                    Evidence report
                  </p>
                  <h2 className="mt-1 font-semibold text-ui">
                    {turns.length}{" "}
                    {turns.length === 1 ? "inquiry" : "inquiries"}
                  </h2>
                </div>
                <span className="flex items-center gap-1.5 text-badge text-fg-subtle">
                  <LockKeyhole className="size-3" /> Branch snapshot
                </span>
              </header>
              {turns.map((turn, index) => (
                <SearchExchange
                  index={index + 1}
                  key={turn.id}
                  onOpenCitation={onOpenCitation}
                  onOpenEvidence={onOpenEvidence}
                  turn={turn}
                />
              ))}
            </div>
          ) : (
            <EmptyInvestigation />
          )}
        </div>
      </div>

      <form
        className="shrink-0 border-border border-t bg-surface p-3"
        onSubmit={onSubmit}
      >
        <div className="mx-auto max-w-3xl">
          <div className="mb-2 flex items-center justify-between">
            <label
              className="font-medium text-badge text-fg-muted uppercase tracking-[0.1em]"
              htmlFor="search-question"
            >
              New inquiry
            </label>
            <span className="flex items-center gap-1 text-badge text-fg-subtle">
              <BookOpenCheck className="size-3" /> Claims require source lines
            </span>
          </div>
          <div className="flex items-end gap-2 border border-border-strong bg-inset p-2 focus-within:border-focus">
            <textarea
              aria-label="Repository question"
              className="min-h-12 flex-1 resize-y bg-transparent px-1 text-fg text-ui outline-none placeholder:text-fg-subtle disabled:opacity-60"
              disabled={disabled || pending}
              id="search-question"
              onChange={(event) => onQuestionChange(event.target.value)}
              onKeyDown={submitShortcut}
              placeholder="Trace a behavior, decision, or dependency through the repository…"
              value={question}
            />
            <Button
              disabled={disabled || pending || !question.trim()}
              type="submit"
            >
              <ArrowUp aria-hidden="true" className="size-3.5" />
              {pending ? "Searching…" : "Ask"}
            </Button>
          </div>
          <p className="mt-1 text-badge text-fg-subtle">
            ⌘↵ or Ctrl↵ to investigate · repository access is read-only
          </p>
        </div>
      </form>
    </section>
  );
}
