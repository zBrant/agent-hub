import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, Workflow } from "lucide-react";
import { useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type DashboardPeriod } from "@/api/client";
import { SystemHealth } from "@/components/dashboard/SystemHealth";
import { TokenBreakdown } from "@/components/dashboard/TokenBreakdown";
import { useSystemMetricsStore } from "@/stores/system-metrics-store";
import { METRICS_TOPIC } from "@/ws/protocol";
import { useWebSocketClient } from "@/ws/WebSocketProvider";

const compact = new Intl.NumberFormat("en", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const periods: readonly [DashboardPeriod, string][] = [
  ["today", "Today"],
  ["7d", "7 days"],
  ["30d", "30 days"],
];

function elapsed(milliseconds: number): string {
  const minutes = Math.floor(milliseconds / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

export function DashboardRoute() {
  const [period, setPeriod] = useState<DashboardPeriod>("today");
  const websocket = useWebSocketClient();
  const latestSystemMetrics = useSystemMetricsStore((state) => state.latest);
  const pushSystemMetrics = useSystemMetricsStore((state) => state.push);
  const dashboard = useQuery({
    queryKey: ["dashboard", period],
    queryFn: () => api.getDashboard(period),
  });

  useEffect(() => {
    if (!websocket) return;
    return websocket.subscribe(METRICS_TOPIC, (_payload, frame) => {
      if (frame.type === "metrics") pushSystemMetrics(frame.payload);
    });
  }, [pushSystemMetrics, websocket]);

  if (dashboard.isLoading) {
    return <p className="p-4 text-meta text-fg-muted">Loading dashboard…</p>;
  }
  if (dashboard.error) {
    return (
      <p className="p-4 text-meta text-failed">{dashboard.error.message}</p>
    );
  }
  if (!dashboard.data) return null;
  const data = dashboard.data;
  const cost = data.usage.estimated_equivalent_cost_usd;

  return (
    <div className="mx-auto max-w-7xl p-4">
      <header className="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="font-semibold text-title">Dashboard</h1>
          <p className="text-meta text-fg-muted">Active work and local usage</p>
        </div>
        <fieldset className="flex rounded-md border border-border bg-surface p-0.5">
          <legend className="sr-only">Dashboard period</legend>
          {periods.map(([value, label]) => (
            <button
              aria-pressed={period === value}
              className="h-6 rounded-sm px-2 text-meta text-fg-muted hover:text-fg aria-pressed:bg-elevated aria-pressed:text-fg"
              key={value}
              onClick={() => setPeriod(value)}
              type="button"
            >
              {label}
            </button>
          ))}
        </fieldset>
      </header>

      <section
        aria-label="Key metrics"
        className="mb-4 grid gap-2 sm:grid-cols-2 xl:grid-cols-5"
      >
        <Kpi
          label="Total tokens"
          value={compact.format(data.usage.tokens.total_tokens)}
        />
        <Kpi
          label="Estimated equivalent cost"
          value={cost == null ? "—" : `$${cost.toFixed(4)}`}
          {...(!data.usage.cost_complete
            ? { detail: "Partial — unpriced usage exists" }
            : {})}
        />
        <Kpi
          label="Active sessions"
          value={String(data.active_session_count)}
        />
        <Kpi
          label="Running / blocked"
          value={`${data.running_node_count} / ${data.blocked_node_count}`}
        />
        <Kpi
          label="Node completion rate"
          value={
            data.node_completion_rate == null
              ? "—"
              : `${Math.round(data.node_completion_rate * 100)}%`
          }
        />
      </section>

      <div className="mb-4 grid gap-3 lg:grid-cols-2">
        <TokenBreakdown rows={data.by_harness} title="Tokens by harness" />
        <TokenBreakdown rows={data.by_model} title="Tokens by model" />
      </div>

      <SystemHealth snapshot={latestSystemMetrics} />

      <section aria-labelledby="active-sessions-heading">
        <h2 className="mb-2 font-semibold text-ui" id="active-sessions-heading">
          Active sessions
        </h2>
        {data.active_sessions.length ? (
          <div className="overflow-hidden rounded-lg border border-border bg-surface">
            {data.active_sessions.map((session) => (
              <Link
                className="grid min-h-14 grid-cols-[minmax(0,1fr)_auto] items-center gap-x-3 border-border border-b px-3 py-2 last:border-b-0 hover:bg-elevated sm:grid-cols-[minmax(0,1fr)_auto_auto_auto]"
                key={session.id}
                to={`/sessions/${session.id}`}
              >
                <div className="flex min-w-0 items-center gap-2">
                  <Workflow className="size-4 shrink-0 text-fg-muted" />
                  <div className="min-w-0">
                    <p className="truncate text-ui">{session.title}</p>
                    <p className="text-meta text-fg-subtle">
                      {session.completed_nodes}/{session.total_nodes} nodes ·{" "}
                      {elapsed(session.elapsed_ms)}
                    </p>
                  </div>
                </div>
                <div className="hidden gap-1 sm:flex">
                  {session.harnesses.map((harness) => (
                    <span
                      className="rounded-sm bg-inset px-1.5 py-0.5 text-badge text-fg-muted"
                      key={harness}
                    >
                      {harness}
                    </span>
                  ))}
                </div>
                <span className="hidden font-mono text-code text-fg-muted sm:block">
                  {compact.format(session.usage.tokens.total_tokens)} tokens
                </span>
                <span className="flex items-center gap-2">
                  {session.blocked_nodes ? (
                    <span className="flex items-center gap-1 text-blocked text-meta">
                      <AlertTriangle className="size-3.5" />
                      {session.blocked_nodes}
                    </span>
                  ) : null}
                  <ArrowRight className="size-4 text-fg-subtle" />
                </span>
              </Link>
            ))}
          </div>
        ) : (
          <div className="rounded-lg border border-border bg-surface p-6 text-center">
            <p className="text-ui">No active sessions</p>
            <p className="text-meta text-fg-subtle">
              New proposals and running graphs will appear here.
            </p>
          </div>
        )}
      </section>
    </div>
  );
}

function Kpi({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <div className="rounded-lg border border-border bg-surface px-3 py-2">
      <p className="text-meta text-fg-muted">{label}</p>
      <p className="font-mono text-title">{value}</p>
      {detail ? <p className="text-badge text-review">{detail}</p> : null}
    </div>
  );
}
