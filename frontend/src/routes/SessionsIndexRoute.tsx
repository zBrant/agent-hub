import { useMutation, useQuery } from "@tanstack/react-query";
import { Workflow } from "lucide-react";
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
    <div className="mx-auto max-w-5xl p-4">
      <h1 className="mb-3 font-semibold text-title">Sessions</h1>

      <PlanGraphForm
        error={plan.error}
        isPending={plan.isPending}
        onSubmit={plan.mutate}
        options={plannerOptions.data}
        optionsError={plannerOptions.error}
        optionsLoading={plannerOptions.isLoading}
      />

      {sessions.isLoading ? (
        <p className="text-meta text-fg-muted">Loading sessions…</p>
      ) : sessions.error ? (
        <p className="text-meta text-failed">{sessions.error.message}</p>
      ) : !sessions.data?.length ? (
        <EmptyState
          icon={Workflow}
          title="No sessions yet"
          description="Create your first graph proposal above."
        />
      ) : (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          {sessions.data.map((session) => (
            <Link
              key={session.id}
              to={`/sessions/${session.id}`}
              className="flex h-10 items-center gap-3 border-border border-b px-3 last:border-b-0 hover:bg-elevated"
            >
              <Workflow className="size-4 text-fg-muted" />
              <span className="min-w-0 flex-1 truncate">{session.title}</span>
              <code className="hidden text-code text-fg-subtle sm:block">
                {session.id}
              </code>
              <span className="text-meta text-fg-muted">{session.status}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
