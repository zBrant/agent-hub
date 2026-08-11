import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowUpRight, GitBranch, Plus, Workflow } from "lucide-react";
import { Link, useNavigate } from "react-router";
import { api } from "@/api/client";
import { EmptyState } from "@/components/layout/EmptyState";
import { PlanGraphForm } from "@/components/sessions/PlanGraphForm";

export function SessionsIndexRoute() {
  const navigate = useNavigate();
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: api.listSessions,
  });
  const plannerOptions = useQuery({
    queryKey: ["planner-options"],
    queryFn: api.getPlannerOptions,
  });
  const plan = useMutation({
    mutationFn: api.planGraph,
    onSuccess: (proposal) => {
      void navigate(`/sessions/${proposal.session.id}`);
    },
  });

  return (
    <div className="flex h-full min-h-0 bg-bg">
      <aside className="hidden w-72 shrink-0 flex-col border-border border-r bg-surface/70 lg:flex">
        <div className="flex h-14 items-center justify-between border-border border-b px-4">
          <div>
            <p className="font-semibold text-ui">Session index</p>
            <p className="text-badge text-fg-subtle">
              All orchestration history
            </p>
          </div>
          <a
            aria-label="New graph proposal"
            className="flex size-7 items-center justify-center rounded-sm border border-border bg-inset text-fg-muted hover:border-border-strong hover:text-fg"
            href="#new-graph"
          >
            <Plus className="size-3.5" />
          </a>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {sessions.isLoading ? (
            <p className="px-2 py-3 text-meta text-fg-muted">
              Loading sessions…
            </p>
          ) : sessions.error ? (
            <p className="px-2 py-3 text-meta text-failed">
              {sessions.error.message}
            </p>
          ) : !sessions.data?.length ? (
            <EmptyState
              icon={Workflow}
              title="No sessions yet"
              description="Your graph history will collect here."
            />
          ) : (
            <nav aria-label="Sessions" className="grid gap-1">
              {sessions.data.map((session) => (
                <Link
                  key={session.id}
                  to={`/sessions/${session.id}`}
                  className="group grid grid-cols-[3px_minmax(0,1fr)_auto] items-center gap-2 rounded-sm px-2 py-2 hover:bg-elevated"
                >
                  <span className="h-7 w-[3px] rounded-full bg-border-strong group-hover:bg-accent" />
                  <span className="min-w-0">
                    <span className="block truncate text-ui">
                      {session.title}
                    </span>
                    <span className="block truncate font-mono text-badge text-fg-subtle">
                      {session.id}
                    </span>
                  </span>
                  <span className="text-badge text-fg-muted">
                    {session.status}
                  </span>
                </Link>
              ))}
            </nav>
          )}
        </div>
      </aside>

      <main className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
        <header className="shrink-0 border-border border-b bg-surface/40 px-4 py-3 sm:px-6">
          <div className="flex items-end justify-between gap-4">
            <div>
              <p className="mb-1 flex items-center gap-1.5 font-mono text-badge uppercase tracking-[0.14em] text-accent">
                <GitBranch className="size-3" /> Orchestrator
              </p>
              <h1 className="font-semibold text-title">New execution graph</h1>
              <p className="mt-1 max-w-2xl text-meta text-fg-muted">
                Turn an objective into isolated, reviewable worktrees. The graph
                remains locked until you approve it.
              </p>
            </div>
            <Link
              className="hidden items-center gap-1 text-meta text-fg-muted hover:text-fg lg:flex"
              to="/dashboard"
            >
              Operations overview <ArrowUpRight className="size-3.5" />
            </Link>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <PlanGraphForm
            error={plan.error}
            isPending={plan.isPending}
            onSubmit={plan.mutate}
            options={plannerOptions.data}
            optionsError={plannerOptions.error}
            optionsLoading={plannerOptions.isLoading}
          />

          <section className="border-border border-t p-4 lg:hidden">
            <h2 className="mb-2 font-semibold text-ui">Recent sessions</h2>
            {sessions.data?.length ? (
              <div className="divide-y divide-border border-y border-border">
                {sessions.data.map((session) => (
                  <Link
                    key={session.id}
                    to={`/sessions/${session.id}`}
                    className="flex items-center gap-3 py-3"
                  >
                    <Workflow className="size-4 text-fg-muted" />
                    <span className="min-w-0 flex-1 truncate">
                      {session.title}
                    </span>
                    <span className="text-meta text-fg-muted">
                      {session.status}
                    </span>
                  </Link>
                ))}
              </div>
            ) : null}
          </section>
        </div>
      </main>
    </div>
  );
}
