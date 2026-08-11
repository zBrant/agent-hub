import { Sparkles } from "lucide-react";
import { type FormEvent, useState } from "react";
import type {
  PlanGraphRequest,
  PlannerOption,
  PlannerOptions,
} from "@/api/client";
import { PlannerChooser } from "@/components/sessions/PlannerChooser";
import { Button } from "@/components/ui/button";
import {
  type PlannerSelection,
  plannerRequestChoice,
  resolvePlannerSelection,
  selectPlannerModel,
  selectPlannerOption,
} from "@/lib/planner-selection";

type Props = {
  error: Error | null;
  isPending: boolean;
  onSubmit: (request: PlanGraphRequest) => void;
  options: PlannerOptions | undefined;
  optionsError: Error | null;
  optionsLoading: boolean;
};

const CONTROL =
  "h-[30px] rounded-md border border-border bg-inset px-2 text-ui text-fg outline-none focus:border-focus";

export function PlanGraphForm({
  error,
  isPending,
  onSubmit,
  options,
  optionsError,
  optionsLoading,
}: Props) {
  const [repoPath, setRepoPath] = useState("");
  const [objective, setObjective] = useState("");
  const [picked, setPicked] = useState<PlannerSelection | null>(null);
  const selection = resolvePlannerSelection(options, picked);
  const awaitingChoice = Boolean(options) && selection === null;

  function pickOption(option: PlannerOption) {
    setPicked(selectPlannerOption(option, selection));
  }

  function pickModel(model: string) {
    if (selection) setPicked(selectPlannerModel(selection, model));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleanRepo = repoPath.trim();
    const cleanObjective = objective.trim();
    if (!cleanRepo || !cleanObjective || awaitingChoice) return;
    const planner = plannerRequestChoice(options, selection);
    onSubmit({
      repo_path: cleanRepo,
      objective: cleanObjective,
      auto_merge: false,
      base_ref: "HEAD",
      context: null,
      ...(planner === null ? {} : { planner }),
    });
  }

  return (
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
            className={CONTROL}
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

      <PlannerChooser
        data={options}
        error={optionsError}
        isLoading={optionsLoading}
        onModelChange={pickModel}
        onOptionChange={pickOption}
        selection={selection}
      />

      <div className="mt-3 flex items-center justify-between gap-3">
        <p className="text-meta text-fg-subtle">
          The planner creates a proposal. Nothing runs before you review and
          approve it.
        </p>
        <Button
          disabled={isPending || optionsLoading || awaitingChoice}
          type="submit"
        >
          {isPending ? "Planning…" : "Create proposal"}
        </Button>
      </div>
      {error ? (
        <p className="mt-3 text-meta text-failed" role="alert">
          {error.message}
        </p>
      ) : null}
    </form>
  );
}
