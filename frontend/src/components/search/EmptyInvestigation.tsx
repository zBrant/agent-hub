import { BookOpenCheck, CheckCircle2, Files } from "lucide-react";

export function EmptyInvestigation() {
  return (
    <div className="flex h-full min-h-64 items-center justify-center p-6">
      <div className="w-full max-w-md border-border border-l pl-4">
        <BookOpenCheck className="mb-3 size-6 text-accent" />
        <p className="font-medium text-badge text-fg-subtle uppercase tracking-[0.12em]">
          Evidence-backed code investigation
        </p>
        <h2 className="mt-1 font-semibold text-title">
          Ask the repository, inspect the proof
        </h2>
        <p className="mt-2 text-fg-muted text-meta leading-relaxed">
          The search agent traces text, syntax, and symbols. Supported claims
          remain linked to the exact source lines it read.
        </p>
        <div className="mt-4 grid grid-cols-2 gap-px border border-border bg-border text-meta">
          <div className="bg-surface p-3">
            <Files className="mb-2 size-3.5 text-fg-subtle" />
            Follow implementations across files
          </div>
          <div className="bg-surface p-3">
            <CheckCircle2 className="mb-2 size-3.5 text-fg-subtle" />
            Validate every rendered claim
          </div>
        </div>
      </div>
    </div>
  );
}
