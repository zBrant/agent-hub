import { Cpu, HardDrive, MemoryStick, Radio, ServerCog } from "lucide-react";
import type { ReactNode } from "react";
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
  return (
    <section aria-labelledby="system-health-heading" className="min-w-0">
      <div className="mb-2 flex items-end justify-between gap-3">
        <div>
          <p className="font-mono text-badge uppercase tracking-[0.14em] text-fg-subtle">
            Host
          </p>
          <h2 className="font-semibold text-ui" id="system-health-heading">
            System health
          </h2>
        </div>
        {snapshot ? (
          <p className="inline-flex items-center gap-1.5 text-badge text-running">
            <Radio className="size-3" />
            Live · {new Date(snapshot.ts).toLocaleTimeString()}
          </p>
        ) : null}
      </div>

      {!snapshot ? (
        <div className="border border-dashed border-border bg-surface px-4 py-8 text-center text-meta text-fg-muted">
          Waiting for the first live system sample…
        </div>
      ) : (
        <SystemSnapshot snapshot={snapshot} />
      )}
    </section>
  );
}

function SystemSnapshot({ snapshot }: { snapshot: SystemMetricsPayload }) {
  const peakCore = Math.max(0, ...snapshot.cpu_per_core);
  return (
    <div className="border border-border bg-surface">
      <div className="grid grid-cols-2 border-border border-b sm:grid-cols-4">
        <Gauge
          detail={`${snapshot.cpu_per_core.length} cores · peak ${percent.format(peakCore)}%`}
          icon={<Cpu className="size-3.5" />}
          label="CPU"
          value={snapshot.cpu_percent}
        />
        <Gauge
          detail={`${formatBytes(snapshot.memory_used_bytes)} / ${formatBytes(snapshot.memory_total_bytes)}`}
          icon={<MemoryStick className="size-3.5" />}
          label="Memory"
          value={snapshot.memory_percent}
        />
        <Gauge
          detail={`${formatBytes(snapshot.swap_used_bytes)} / ${formatBytes(snapshot.swap_total_bytes)}`}
          icon={<ServerCog className="size-3.5" />}
          label="Swap"
          value={snapshot.swap_percent}
        />
        <Gauge
          detail={`${formatBytes(snapshot.disk_used_bytes)} / ${formatBytes(snapshot.disk_total_bytes)}`}
          icon={<HardDrive className="size-3.5" />}
          label="Worktree disk"
          value={snapshot.disk_percent}
        />
      </div>

      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left text-meta">
          <caption className="sr-only">Active agent process trees</caption>
          <thead className="text-badge uppercase tracking-wide text-fg-subtle">
            <tr>
              <th className="px-3 py-2 font-medium" scope="col">
                Process tree
              </th>
              <th className="px-2 py-2 font-medium" scope="col">
                Harness
              </th>
              <th className="px-2 py-2 text-right font-medium" scope="col">
                PID
              </th>
              <th className="px-2 py-2 text-right font-medium" scope="col">
                Procs
              </th>
              <th className="px-2 py-2 text-right font-medium" scope="col">
                CPU
              </th>
              <th className="px-2 py-2 text-right font-medium" scope="col">
                RSS
              </th>
              <th className="px-3 py-2 text-right font-medium" scope="col">
                Uptime
              </th>
            </tr>
          </thead>
          <tbody>
            {snapshot.processes.length ? (
              snapshot.processes.map((process) => (
                <tr
                  className="border-border border-t hover:bg-elevated"
                  key={process.node_id}
                >
                  <td className="px-3 py-2 font-mono text-code text-fg">
                    {process.node_id}
                  </td>
                  <td className="px-2 py-2 font-mono text-code text-fg-muted">
                    {process.harness}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-code">
                    {process.pid}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-code">
                    {process.process_count}
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-code">
                    {percent.format(process.cpu_percent)}%
                  </td>
                  <td className="px-2 py-2 text-right font-mono text-code">
                    {formatBytes(process.rss_bytes)}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-code">
                    {formatUptime(process.uptime_ms)}
                  </td>
                </tr>
              ))
            ) : (
              <tr className="border-border border-t">
                <td
                  className="px-3 py-6 text-center text-fg-subtle"
                  colSpan={7}
                >
                  No active agent processes.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Gauge({
  detail,
  icon,
  label,
  value,
}: {
  detail: string;
  icon: ReactNode;
  label: string;
  value: number;
}) {
  const bounded = Math.max(0, Math.min(100, value));
  const meterColor =
    bounded >= 90 ? "bg-failed" : bounded >= 75 ? "bg-review" : "bg-running";
  return (
    <div className="min-w-0 border-border p-2.5 max-sm:[&:nth-child(-n+2)]:border-b max-sm:[&:nth-child(odd)]:border-r sm:border-r sm:last:border-r-0">
      <div className="flex items-center justify-between gap-2">
        <p className="flex items-center gap-1.5 text-badge text-fg-muted">
          {icon}
          {label}
        </p>
        <p className="font-mono text-meta text-fg">{percent.format(value)}%</p>
      </div>
      <div
        aria-label={`${label} utilization`}
        aria-valuemax={100}
        aria-valuemin={0}
        aria-valuenow={bounded}
        className="my-2 h-1 overflow-hidden bg-inset"
        role="progressbar"
      >
        <div
          className={`h-full ${meterColor}`}
          style={{ width: `${bounded}%` }}
        />
      </div>
      <p className="truncate font-mono text-badge text-fg-subtle">{detail}</p>
    </div>
  );
}
