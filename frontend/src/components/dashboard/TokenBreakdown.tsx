import type { MetricUsage } from "@/api/client";

type Props = {
  title: string;
  rows: readonly MetricUsage[];
};

const compact = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const categories = [
  ["Cache read", "cache_read_tokens", "bg-token-cache-read"],
  ["Cache write", "cache_write_tokens", "bg-token-cache-write"],
  ["Input", "input_tokens", "bg-token-input"],
  ["Output", "output_tokens", "bg-token-output"],
] as const;

export function TokenBreakdown({ title, rows }: Props) {
  return (
    <section className="min-w-0 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h3 className="font-medium text-meta text-fg-muted">{title}</h3>
        <span className="font-mono text-badge text-fg-subtle">
          {rows.length} {rows.length === 1 ? "source" : "sources"}
        </span>
      </div>
      {rows.length ? (
        <div className="divide-y divide-border">
          {rows.map((row) => (
            <div className="py-3 first:pt-0 last:pb-0" key={row.key}>
              <div className="mb-2 flex items-center justify-between gap-3">
                <span className="truncate font-mono text-code text-fg">
                  {row.key}
                </span>
                <span className="font-mono text-code text-fg-muted">
                  {compact.format(row.tokens.total_tokens)}
                </span>
              </div>
              <div
                aria-label={`${row.key} token mix`}
                className="flex h-1.5 overflow-hidden bg-inset"
                role="img"
              >
                {categories.map(([label, key, color]) => {
                  const value = row.tokens[key];
                  return value ? (
                    <span
                      className={color}
                      key={key}
                      style={{ flexGrow: value }}
                      title={`${label}: ${value.toLocaleString("en")}`}
                    />
                  ) : null;
                })}
              </div>
              <dl className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 sm:grid-cols-4">
                {categories.map(([label, key, color]) => (
                  <div className="flex min-w-0 items-center gap-1.5" key={key}>
                    <span className={`size-1.5 shrink-0 ${color}`} />
                    <dt className="truncate text-badge text-fg-subtle">
                      {label}
                    </dt>
                    <dd className="ml-auto font-mono text-badge text-fg-muted">
                      {compact.format(row.tokens[key])}
                    </dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
      ) : (
        <p className="py-2 text-meta text-fg-subtle">
          No usage in this period.
        </p>
      )}
    </section>
  );
}
