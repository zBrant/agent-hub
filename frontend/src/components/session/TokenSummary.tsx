import type { RunSummary } from "@/api/client";

type Props = {
  summary: RunSummary | null;
};

const number = new Intl.NumberFormat("en", { notation: "compact" });

export function TokenSummary({ summary }: Props) {
  const tokens = summary?.tokens;
  const values = [
    ["Cache read", tokens?.cache_read_tokens ?? 0],
    ["Cache write", tokens?.cache_write_tokens ?? 0],
    ["Input", tokens?.input_tokens ?? 0],
    ["Output", tokens?.output_tokens ?? 0],
  ] as const;

  return (
    <section
      aria-labelledby="usage-heading"
      className="border-border border-b bg-surface"
    >
      <div className="flex items-center justify-between px-3 py-2.5">
        <h2 id="usage-heading" className="font-semibold text-ui">
          Usage
        </h2>
        <span className="font-mono text-code text-fg-muted">
          {number.format(tokens?.total_tokens ?? 0)} tokens
        </span>
      </div>
      <dl className="grid grid-cols-2 border-border border-t bg-inset/35 sm:grid-cols-4">
        {values.map(([label, value]) => (
          <div
            key={label}
            className="border-border border-r px-3 py-2.5 last:border-r-0"
          >
            <dt className="text-meta text-fg-muted">{label}</dt>
            <dd className="font-mono text-code text-fg">
              {number.format(value)}
            </dd>
          </div>
        ))}
      </dl>
      <div className="flex items-center justify-between border-border border-t px-3 py-2 text-meta">
        <span className="text-fg-muted">Estimated equivalent cost</span>
        <span className="font-mono text-code text-fg">
          {summary?.estimated_equivalent_cost_usd == null
            ? "—"
            : `$${summary.estimated_equivalent_cost_usd.toFixed(4)}`}
          {summary && !summary.cost_complete ? " (partial)" : ""}
        </span>
      </div>
    </section>
  );
}
