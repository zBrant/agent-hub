import type { SearchTurn } from "@/components/search/SearchExchange";

type Props = {
  turns: readonly SearchTurn[];
};

export function InquiryIndex({ turns }: Props) {
  return (
    <aside
      aria-label="Inquiry index"
      className="hidden w-40 shrink-0 flex-col border-border border-r bg-surface xl:flex"
    >
      <p className="border-border border-b px-3 py-3 font-medium text-badge text-fg-subtle uppercase tracking-[0.12em]">
        Inquiry index
      </p>
      <nav className="min-h-0 flex-1 overflow-y-auto py-1">
        {turns.map((turn, index) => (
          <a
            className="flex gap-2 border-border border-l-2 px-3 py-2 text-fg-muted hover:border-accent hover:bg-elevated hover:text-fg"
            href={`#investigation-${turn.id}`}
            key={turn.id}
          >
            <span className="font-mono text-badge text-fg-subtle">
              {String(index + 1).padStart(2, "0")}
            </span>
            <span className="line-clamp-2 text-meta leading-relaxed">
              {turn.question}
            </span>
          </a>
        ))}
      </nav>
    </aside>
  );
}
