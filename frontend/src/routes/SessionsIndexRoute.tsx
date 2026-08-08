import { useMutation, useQuery } from "@tanstack/react-query";
import { Sparkles, Workflow } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useNavigate } from "react-router";
import { api } from "@/api/client";
import { EmptyState } from "@/components/layout/EmptyState";
import { Button } from "@/components/ui/button";

export function SessionsIndexRoute() {
  const navigate = useNavigate();
  const [repoPath, setRepoPath] = useState("");
  const [objective, setObjective] = useState("");
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: api.listSessions,
  });
  const plan = useMutation({
    mutationFn: api.planGraph,
    onSuccess: (proposal) => {
      void navigate(`/sessions/${proposal.session.id}`);
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleanRepo = repoPath.trim();
    const cleanObjective = objective.trim();
    if (!cleanRepo || !cleanObjective) return;
    plan.mutate({
      repo_path: cleanRepo,
      objective: cleanObjective,
      auto_merge: false,
      base_ref: "HEAD",
      context: null,
    });
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <h1 className="mb-3 font-semibold text-title">Sessions</h1>

      <form
        className="mb-6 rounded-lg border border-border bg-surface p-4"
        onSubmit={submit}
      >
        <div className="mb-3 flex items-center gap-2">
          <Sparkles className="size-4 text-accent" />
          <h2 className="font-medium">Plan a graph</h2>
        </div>
        <div className="grid gap-3 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
          <label className="grid gap-1 text-meta text-fg-muted">
            Repository path
            <input
              className="h-[30px] rounded-md border border-border bg-inset px-2 text-ui text-fg outline-none focus:border-focus"
              onChange={(event) => setRepoPath(event.target.value)}
              placeholder="/Users/me/project"
              required
              value={repoPath}
            />
          </label>
          <label className="grid gap-1 text-meta text-fg-muted">
            Objective
            <textarea
              className="min-h-20 resize-y rounded-md border border-border bg-inset px-2 py-1.5 text-ui text-fg outline-none focus:border-focus"
              onChange={(event) => setObjective(event.target.value)}
              placeholder="Describe the outcome you want the agents to build…"
              required
              value={objective}
            />
          </label>
        </div>
        <div className="mt-3 flex items-center justify-between gap-3">
          <p className="text-meta text-fg-subtle">
            The planner creates a proposal. Nothing runs before you review and
            approve it.
          </p>
          <Button disabled={plan.isPending} type="submit">
            {plan.isPending ? "Planning…" : "Create proposal"}
          </Button>
        </div>
        {plan.error ? (
          <p className="mt-3 text-meta text-failed" role="alert">
            {plan.error.message}
          </p>
        ) : null}
      </form>

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
