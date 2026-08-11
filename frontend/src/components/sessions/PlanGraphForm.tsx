import {
  ArrowRight,
  ChevronDown,
  GitBranch,
  LockKeyhole,
  SlidersHorizontal,
} from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link } from "react-router";
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
  const [finalBranch, setFinalBranch] = useState("");
  const [objective, setObjective] = useState("");
  const [picked, setPicked] = useState<PlannerSelection | null>(null);
  const [overridePlanner, setOverridePlanner] = useState(false);
  const selection = resolvePlannerSelection(options, picked);
  const awaitingChoice =
    overridePlanner && Boolean(options) && selection === null;

  function pickOption(option: PlannerOption) {
    setPicked(selectPlannerOption(option, selection));
  }

  function pickModel(model: string) {
    if (selection) setPicked(selectPlannerModel(selection, model));
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const cleanRepo = repoPath.trim();
    const cleanFinalBranch = finalBranch.trim();
    const cleanObjective = objective.trim();
    if (!cleanRepo || !cleanFinalBranch || !cleanObjective || awaitingChoice)
      return;
    const planner = overridePlanner
      ? plannerRequestChoice(options, selection)
      : null;
    onSubmit({
      repo_path: cleanRepo,
      final_branch: cleanFinalBranch,
      objective: cleanObjective,
      auto_merge: false,
      base_ref: "HEAD",
      context: null,
      ...(planner === null ? {} : { planner }),
    });
  }

  return (
    <form
      className="grid min-h-full bg-bg xl:grid-cols-[minmax(0,1fr)_380px]"
      id="new-graph"
      onSubmit={submit}
    >
      <section className="flex min-h-[560px] min-w-0 flex-col border-border xl:border-r">
        <header className="flex items-center justify-between border-border border-b bg-surface px-4 py-3 sm:px-6">
          <div className="flex items-center gap-2">
            <span className="flex size-7 items-center justify-center bg-accent/10 text-accent">
              <GitBranch className="size-4" />
            </span>
            <div>
              <h2 className="font-semibold text-ui">Execution brief</h2>
              <p className="text-badge text-fg-subtle">
                Define the repository and the outcome, not the implementation.
              </p>
            </div>
          </div>
          <span className="hidden items-center gap-1.5 text-badge text-fg-subtle sm:flex">
            <LockKeyhole className="size-3" /> Approval gate enabled
          </span>
        </header>

        <div className="grid content-start gap-4 border-border border-b bg-surface/35 p-4 sm:px-6">
          <label className="grid gap-1 text-meta text-fg-muted">
            <span className="font-medium text-fg">Repository</span>
            <span className="mb-1 text-badge text-fg-subtle">
              Absolute path to the target Git checkout. Every node receives its
              own isolated worktree.
            </span>
            <input
              aria-label="Repository path"
              className={`${CONTROL} max-w-2xl font-mono text-code`}
              onChange={(event) => setRepoPath(event.target.value)}
              placeholder="/Users/me/project"
              required
              value={repoPath}
            />
          </label>

          <label className="grid gap-1 text-meta text-fg-muted">
            <span className="font-medium text-fg">Final branch</span>
            <span className="mb-1 text-badge text-fg-subtle">
              Reserved for this graph and created when execution completes.
              Existing Git branches and unfinished-session reservations are
              rejected.
            </span>
            <input
              aria-label="Final branch"
              autoComplete="off"
              className={`${CONTROL} max-w-2xl font-mono text-code`}
              onChange={(event) => setFinalBranch(event.target.value)}
              placeholder="feature/agenthub-result"
              required
              spellCheck={false}
              value={finalBranch}
            />
          </label>
        </div>

        <label className="flex min-h-0 flex-1 flex-col p-4 text-meta text-fg-muted sm:p-6">
          <span className="font-medium text-fg">Objective</span>
          <span className="mt-1 mb-3 text-badge text-fg-subtle">
            State the result and constraints. The planner will propose activity
            boundaries, dependencies, harnesses, and acceptance criteria.
          </span>
          <textarea
            aria-label="Objective"
            className="min-h-64 flex-1 resize-none border border-border-strong bg-inset p-4 text-ui text-fg leading-relaxed outline-none placeholder:text-fg-subtle focus:border-focus"
            onChange={(event) => setObjective(event.target.value)}
            placeholder="Describe the outcome you want the agents to build…"
            required
            value={objective}
          />
        </label>
      </section>

      <aside className="flex min-h-[480px] flex-col bg-surface">
        <header className="border-border border-b p-3">
          <button
            aria-expanded={overridePlanner}
            className="flex w-full items-center gap-3 border border-border bg-inset p-3 text-left hover:border-border-strong hover:bg-elevated"
            onClick={() => setOverridePlanner((current) => !current)}
            type="button"
          >
            <span className="grid size-8 shrink-0 place-items-center border border-border-strong bg-surface text-accent">
              <SlidersHorizontal aria-hidden="true" className="size-4" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block font-medium text-ui">
                Override planner defaults
              </span>
              <span className="block text-badge text-fg-muted">
                {overridePlanner
                  ? "Choose a runtime for this graph only."
                  : "Using the global AI runtime settings."}
              </span>
            </span>
            <ChevronDown
              aria-hidden="true"
              className={`size-4 text-fg-muted transition-transform ${
                overridePlanner ? "rotate-180" : ""
              }`}
            />
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {overridePlanner ? (
            <div className="p-4">
              <PlannerChooser
                data={options}
                error={optionsError}
                isLoading={optionsLoading}
                onModelChange={pickModel}
                onOptionChange={pickOption}
                selection={selection}
              />
            </div>
          ) : (
            <div className="p-4 text-meta text-fg-muted">
              <p>
                Planner runtime, model, and effort come from your global
                settings unless you override them here.
              </p>
              <Link
                className="mt-2 inline-flex text-accent hover:text-accent-hover"
                to="/settings"
              >
                Manage global defaults
              </Link>
            </div>
          )}
        </div>
        {error ? (
          <p
            className="border-failed border-t bg-failed/10 px-4 py-3 text-meta text-failed"
            role="alert"
          >
            {error.message}
          </p>
        ) : null}
        <footer className="border-border border-t bg-elevated px-4 py-3">
          <p className="mb-3 text-badge text-fg-subtle">
            This creates an editable proposal. No agent starts before explicit
            approval.
          </p>
          <Button
            className="w-full"
            disabled={
              isPending || (overridePlanner && optionsLoading) || awaitingChoice
            }
            type="submit"
          >
            {isPending ? "Planning…" : "Create proposal"}
            {!isPending ? <ArrowRight /> : null}
          </Button>
        </footer>
      </aside>
    </form>
  );
}
