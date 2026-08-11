import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CircleGauge,
  Coins,
  Network,
  Workflow,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, type DashboardPeriod } from "@/api/client";
import { SystemHealth } from "@/components/dashboard/SystemHealth";
import { TokenDistributionDialog } from "@/components/dashboard/TokenDistributionDialog";
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
    return <p className="p-5 text-meta text-fg-muted">Loading dashboard…</p>;
  }
  if (dashboard.error) {
    return (
      <p className="p-5 text-meta text-failed" role="alert">
        {dashboard.error.message}
      </p>
    );
  }
  if (!dashboard.data) return null;

  const data = dashboard.data;
  const cost = data.usage.estimated_equivalent_cost_usd;
  const completion =
    data.node_completion_rate == null
      ? "—"
      : `${Math.round(data.node_completion_rate * 100)}%`;

  return (
    <div className="flex min-h-full flex-col bg-bg">
      <header className="flex min-h-16 flex-wrap items-end justify-between gap-4 border-border border-b bg-surface/55 px-4 py-3 sm:px-5 lg:px-6">
        <div>
          <p className="mb-1 font-mono text-badge uppercase tracking-[0.16em] text-accent">
            Operations
          </p>
          <h1 className="font-semibold text-title">Dashboard</h1>
          <p className="mt-0.5 text-meta text-fg-muted">
            Agent activity, graph progress, and host telemetry
          </p>
        </div>
        <fieldset className="flex border border-border bg-surface p-0.5">
          <legend className="sr-only">Dashboard period</legend>
          {periods.map(([value, label]) => (
            <button
              aria-pressed={period === value}
              className="h-7 px-2.5 text-meta text-fg-muted transition-colors hover:text-fg aria-pressed:bg-elevated aria-pressed:text-fg"
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
        aria-label="Operational status"
        className="grid border-border border-b bg-surface sm:grid-cols-2 lg:grid-cols-5"
      >
        <StatusMetric
          detail={`${data.active_session_count} active ${data.active_session_count === 1 ? "graph" : "graphs"}`}
          icon={<CircleGauge className="size-4" />}
          label="Executing"
          tone="running"
          value={String(data.running_node_count)}
        />
        <StatusMetric
          detail={
            data.blocked_node_count
              ? "Operator action required"
              : "No intervention required"
          }
          icon={<AlertTriangle className="size-4" />}
          label="Needs attention"
          tone={data.blocked_node_count ? "blocked" : "muted"}
          value={String(data.blocked_node_count)}
        />
        <StatusMetric
          detail="Nodes completed"
          icon={<CheckCircle2 className="size-4" />}
          label="Completion rate"
          value={completion}
        />
        <StatusMetric
          detail="Four-field total"
          icon={<Network className="size-4" />}
          label="Token volume"
          value={compact.format(data.usage.tokens.total_tokens)}
        >
          <TokenDistributionDialog
            byHarness={data.by_harness}
            byModel={data.by_model}
            period={period}
            totalTokens={data.usage.tokens.total_tokens}
          />
        </StatusMetric>
        <StatusMetric
          detail={
            data.usage.cost_complete
              ? "Estimated equivalent"
              : "Partial — unpriced usage exists"
          }
          icon={<Coins className="size-4" />}
          label="Equivalent cost"
          tone={!data.usage.cost_complete ? "review" : "muted"}
          value={cost == null ? "—" : `$${cost.toFixed(4)}`}
        />
      </section>

      <div className="grid min-h-0 flex-1 xl:grid-cols-[minmax(0,1.35fr)_minmax(420px,0.65fr)]">
        <div className="min-w-0 border-border p-4 sm:p-5 xl:border-r lg:p-6">
          <ActiveGraphs sessions={data.active_sessions} />
        </div>
        <div className="min-w-0 bg-surface/30 p-4 sm:p-5 lg:p-6">
          <SystemHealth snapshot={latestSystemMetrics} />
        </div>
      </div>
    </div>
  );
}

type StatusTone = "running" | "blocked" | "review" | "muted";

const statusTone: Record<StatusTone, string> = {
  running: "text-running",
  blocked: "text-blocked",
  review: "text-review",
  muted: "text-fg-muted",
};

function StatusMetric({
  children,
  detail,
  icon,
  label,
  tone = "muted",
  value,
}: {
  children?: ReactNode;
  detail: string;
  icon: ReactNode;
  label: string;
  tone?: StatusTone;
  value: string;
}) {
  return (
    <div className="min-w-0 border-border p-3.5 max-sm:border-b max-sm:last:border-b-0 sm:[&:nth-child(-n+4)]:border-b sm:[&:nth-child(odd)]:border-r lg:border-r lg:border-b-0 lg:last:border-r-0">
      <div className={`mb-3 flex items-center gap-1.5 ${statusTone[tone]}`}>
        {icon}
        <p className="text-meta">{label}</p>
      </div>
      <p className="font-mono text-metric tracking-[-0.04em] text-fg">
        {value}
      </p>
      <p className={`mt-2 truncate text-badge ${statusTone[tone]}`}>{detail}</p>
      {children}
    </div>
  );
}

function ActiveGraphs({
  sessions,
}: {
  sessions: Awaited<ReturnType<typeof api.getDashboard>>["active_sessions"];
}) {
  return (
    <section aria-labelledby="active-sessions-heading">
      <div className="mb-2 flex items-end justify-between gap-3">
        <div>
          <p className="font-mono text-badge uppercase tracking-[0.14em] text-fg-subtle">
            Work queue
          </p>
          <h2 className="font-semibold text-ui" id="active-sessions-heading">
            Active graphs
          </h2>
        </div>
        <span className="font-mono text-badge text-fg-subtle">
          {sessions.length} open
        </span>
      </div>

      {sessions.length ? (
        <div className="border border-border bg-surface">
          {sessions.map((session) => {
            const completion = session.total_nodes
              ? Math.round(
                  (session.completed_nodes / session.total_nodes) * 100,
                )
              : 0;
            return (
              <Link
                className="group grid min-h-20 gap-3 border-border border-b px-3.5 py-3 transition-colors last:border-b-0 hover:bg-elevated sm:grid-cols-[minmax(13rem,1.4fr)_minmax(10rem,1fr)_auto] sm:items-center"
                key={session.id}
                to={`/sessions/${session.id}`}
              >
                <div className="flex min-w-0 items-start gap-2.5">
                  <span className="mt-0.5 grid size-7 shrink-0 place-items-center border border-border bg-inset text-fg-muted">
                    <Workflow className="size-4" />
                  </span>
                  <div className="min-w-0">
                    <div className="flex min-w-0 items-center gap-2">
                      <p className="truncate font-medium text-ui text-fg">
                        {session.title}
                      </p>
                      {session.blocked_nodes ? (
                        <span className="inline-flex shrink-0 items-center gap-1 text-badge text-blocked">
                          <AlertTriangle className="size-3" />
                          {session.blocked_nodes} blocked
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-1 font-mono text-badge text-fg-subtle">
                      {session.completed_nodes}/{session.total_nodes} nodes ·{" "}
                      {elapsed(session.elapsed_ms)}
                    </p>
                  </div>
                </div>

                <div className="min-w-0">
                  <div className="mb-1.5 flex items-center justify-between gap-2 text-badge">
                    <span className="text-fg-muted">Graph progress</span>
                    <span className="font-mono text-fg">{completion}%</span>
                  </div>
                  <div
                    aria-label={`${session.title} completion`}
                    aria-valuemax={100}
                    aria-valuemin={0}
                    aria-valuenow={completion}
                    className="h-1 overflow-hidden bg-inset"
                    role="progressbar"
                  >
                    <div
                      className="h-full bg-done"
                      style={{ width: `${completion}%` }}
                    />
                  </div>
                </div>

                <div className="flex items-center justify-between gap-4 sm:justify-end">
                  <div className="text-right">
                    <p className="font-mono text-code text-fg-muted">
                      {compact.format(session.usage.tokens.total_tokens)} tokens
                    </p>
                    <p className="mt-1 truncate text-badge text-fg-subtle">
                      {session.harnesses.join(" · ") || "No harness"}
                    </p>
                  </div>
                  <ArrowRight className="size-4 text-fg-subtle transition-transform group-hover:translate-x-0.5 group-hover:text-fg" />
                </div>
              </Link>
            );
          })}
        </div>
      ) : (
        <div className="border border-dashed border-border bg-surface px-4 py-8 text-center">
          <p className="text-ui">No active sessions</p>
          <p className="mt-1 text-meta text-fg-subtle">
            New proposals and running graphs will appear here.
          </p>
        </div>
      )}
    </section>
  );
}
