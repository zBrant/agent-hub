import { useQuery } from "@tanstack/react-query";
import { Workflow } from "lucide-react";
import { Link } from "react-router";
import { api } from "@/api/client";
import { EmptyState } from "@/components/layout/EmptyState";

export function SessionsIndexRoute() {
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: api.listSessions,
  });
  if (sessions.isLoading) {
    return <p className="p-4 text-meta text-fg-muted">Loading sessions…</p>;
  }
  if (sessions.error) {
    return (
      <p className="p-4 text-meta text-failed">{sessions.error.message}</p>
    );
  }
  if (!sessions.data?.length) {
    return (
      <EmptyState
        icon={Workflow}
        title="No sessions yet"
        description="Create a session through the orchestrator API, then it will appear here with its live run state."
      />
    );
  }
  return (
    <div className="p-4">
      <h1 className="mb-3 font-semibold text-title">Sessions</h1>
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
    </div>
  );
}
