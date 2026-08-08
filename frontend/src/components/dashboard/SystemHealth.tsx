import type { SystemMetricsPayload } from "@/ws/protocol";

type Props = {
  snapshot: SystemMetricsPayload | null;
};

const percent = new Intl.NumberFormat("en", { maximumFractionDigits: 1 });

function formatBytes(bytes: number): string {
  if (bytes < 1_024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"] as const;
  let value = bytes / 1_024;
  let unit: (typeof units)[number] = units[0];
  for (const candidate of units.slice(1)) {
    if (value < 1_024) break;
    value /= 1_024;
    unit = candidate;
  }
  return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}`;
}

function formatUptime(milliseconds: number): string {
  const seconds = Math.floor(milliseconds / 1_000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

export function SystemHealth({ snapshot }: Props) {
  if (!snapshot) {
    return (
      <section aria-labelledby="system-health-heading" className="mb-4">
        <h2 className="mb-2 font-semibold text-ui" id="system-health-heading">
          System health
        </h2>
        <div className="rounded-lg border border-border bg-surface p-3 text-meta text-fg-muted">
          Waiting for the first live system sample…
        </div>
      </section>
    );
  }

  const peakCore = Math.max(0, ...snapshot.cpu_per_core);
  return (
    <section aria-labelledby="system-health-heading" className="mb-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="font-semibold text-ui" id="system-health-heading">
          System health
        </h2>
        <p className="text-meta text-fg-subtle">
          Live · {new Date(snapshot.ts).toLocaleTimeString()}
        </p>
      </div>

      <div className="mb-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <Gauge
          detail={`${snapshot.cpu_per_core.length} cores · peak ${percent.format(peakCore)}%`}
          label="CPU"
          value={snapshot.cpu_percent}
        />
        <Gauge
          detail={`${formatBytes(snapshot.memory_used_bytes)} / ${formatBytes(snapshot.memory_total_bytes)}`}
          label="Memory"
          value={snapshot.memory_percent}
        />
        <Gauge
          detail={`${formatBytes(snapshot.swap_used_bytes)} / ${formatBytes(snapshot.swap_total_bytes)}`}
          label="Swap"
          value={snapshot.swap_percent}
        />
        <Gauge
          detail={`${formatBytes(snapshot.disk_used_bytes)} / ${formatBytes(snapshot.disk_total_bytes)}`}
          label="Worktree disk"
          value={snapshot.disk_percent}
        />
      </div>

      <div className="overflow-x-auto rounded-lg border border-border bg-surface">
        <table className="w-full border-collapse text-left text-meta">
          <caption className="sr-only">Active agent process trees</caption>
          <thead className="bg-elevated text-fg-muted">
            <tr>
              <th className="px-2 py-1.5 font-medium" scope="col">
                Node
              </th>
              <th className="px-2 py-1.5 font-medium" scope="col">
                Harness
              </th>
              <th className="px-2 py-1.5 text-right font-medium" scope="col">
                PID
              </th>
              <th className="px-2 py-1.5 text-right font-medium" scope="col">
                Processes
              </th>
              <th className="px-2 py-1.5 text-right font-medium" scope="col">
                CPU
              </th>
              <th className="px-2 py-1.5 text-right font-medium" scope="col">
                RSS
              </th>
              <th className="px-2 py-1.5 text-right font-medium" scope="col">
                Uptime
              </th>
            </tr>
          </thead>
          <tbody>
            {snapshot.processes.length ? (
              snapshot.processes.map((process) => (
                <tr className="border-border border-t" key={process.node_id}>
                  <td className="px-2 py-1.5 font-mono text-fg">
                    {process.node_id}
                  </td>
                  <td className="px-2 py-1.5 font-mono text-fg-muted">
                    {process.harness}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono">
                    {process.pid}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono">
                    {process.process_count}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono">
                    {percent.format(process.cpu_percent)}%
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono">
                    {formatBytes(process.rss_bytes)}
                  </td>
                  <td className="px-2 py-1.5 text-right font-mono">
                    {formatUptime(process.uptime_ms)}
                  </td>
                </tr>
              ))
            ) : (
              <tr>
                <td
                  className="px-2 py-3 text-center text-fg-subtle"
                  colSpan={7}
                >
                  No active agent processes.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Gauge({
  detail,
  label,
  value,
}: {
  detail: string;
  label: string;
  value: number;
}) {
  const bounded = Math.max(0, Math.min(100, value));
  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-2">
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-meta text-fg-muted">{label}</p>
        <p className="font-mono text-ui">{percent.format(value)}%</p>
      </div>
      <div
        aria-label={`${label} utilization`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={bounded}
        className="my-1 h-1.5 overflow-hidden rounded-sm bg-inset"
        role="progressbar"
      >
        <div className="h-full bg-running" style={{ width: `${bounded}%` }} />
      </div>
      <p className="text-badge text-fg-subtle">{detail}</p>
    </div>
  );
}
