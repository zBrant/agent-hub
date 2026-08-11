import { useMutation, useQuery } from "@tanstack/react-query";
import {
  BadgeCheck,
  Bot,
  CheckCircle2,
  CreditCard,
  SearchCode,
  Settings2,
} from "lucide-react";
import { type FormEvent, useState } from "react";
import {
  type AISettings,
  type AISettingsChoice,
  type AISettingsOption,
  api,
  type PlannerEffort,
} from "@/api/client";
import { Button } from "@/components/ui/button";
import { harnessDotClass } from "@/lib/harness";
import { cn } from "@/lib/utils";

const CONTROL =
  "h-9 w-full border border-border-strong bg-inset px-2.5 text-ui text-fg outline-none focus:border-focus";

const EFFORTS: readonly PlannerEffort[] = [
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
];

type RuntimeFieldProps = {
  choice: AISettingsChoice;
  legend: string;
  name: string;
  onChange: (choice: AISettingsChoice) => void;
  options: readonly AISettingsOption[];
};

function runtimeLabel(option: AISettingsOption) {
  return option.harness ?? "Anthropic API";
}

function RuntimeField({
  choice,
  legend,
  name,
  onChange,
  options,
}: RuntimeFieldProps) {
  const selected =
    options.find(
      (option) =>
        option.backend === choice.backend && option.harness === choice.harness,
    ) ?? null;

  function pick(option: AISettingsOption) {
    const keepsModel =
      choice.model !== null && option.models.includes(choice.model);
    const fallbackModel =
      option.backend === "api" ? (option.models[0] ?? null) : null;
    onChange({
      backend: option.backend,
      harness: option.harness,
      model: keepsModel ? choice.model : fallbackModel,
    });
  }

  return (
    <fieldset className="grid min-w-0 gap-4">
      <legend className="sr-only">{legend}</legend>
      <div className="grid grid-cols-1 gap-px overflow-hidden border border-border bg-border">
        {options.map((option) => {
          const active =
            option.backend === choice.backend &&
            option.harness === choice.harness;
          return (
            <label
              className={cn(
                "relative flex min-h-16 cursor-pointer flex-col justify-center gap-1 bg-inset px-3 py-2.5",
                active
                  ? "bg-accent/8 shadow-[inset_2px_0_var(--color-accent)]"
                  : "hover:bg-elevated",
              )}
              key={`${option.backend}:${option.harness ?? "api"}`}
            >
              <span className="flex items-center gap-2">
                <input
                  checked={active}
                  className="size-3.5 accent-accent"
                  name={name}
                  onChange={() => pick(option)}
                  type="radio"
                />
                <span
                  aria-hidden="true"
                  className={cn(
                    "size-1.5 rounded-full",
                    harnessDotClass(option.harness),
                  )}
                />
                <span className="font-medium text-ui">
                  {runtimeLabel(option)}
                </span>
              </span>
              <span
                className={cn(
                  "flex items-center gap-1.5 pl-[22px] text-badge",
                  option.is_spend ? "text-review" : "text-fg-muted",
                )}
              >
                {option.is_spend ? (
                  <CreditCard aria-hidden="true" className="size-3.5" />
                ) : (
                  <BadgeCheck aria-hidden="true" className="size-3.5" />
                )}
                {option.is_spend
                  ? "Real per-token billing · server-managed credentials"
                  : "Subscription · estimated equivalent only"}
              </span>
            </label>
          );
        })}
      </div>

      <label className="grid min-w-0 gap-1.5 text-meta text-fg-muted">
        Model
        <select
          className={`${CONTROL} font-mono text-code`}
          disabled={!selected}
          onChange={(event) =>
            onChange({
              ...choice,
              model: event.target.value || null,
            })
          }
          value={choice.model ?? ""}
        >
          {selected?.backend === "harness" ? (
            <option value="">CLI default</option>
          ) : null}
          {selected?.models.map((model) => (
            <option key={model} value={model}>
              {model}
            </option>
          ))}
        </select>
      </label>

      {selected?.is_spend ? (
        <p className="flex items-start gap-2 border border-review/30 bg-review/5 p-3 text-meta text-review">
          <CreditCard aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          This runtime uses credentials configured on the AgentHub server. API
          keys are never entered or stored here, and requests incur real
          provider charges.
        </p>
      ) : null}
    </fieldset>
  );
}

function SettingsForm({ initial }: { initial: AISettings }) {
  const [planner, setPlanner] = useState(initial.planner);
  const [search, setSearch] = useState(initial.search);
  const [effort, setEffort] = useState(initial.planner_effort);
  const [changed, setChanged] = useState(false);
  const save = useMutation({
    mutationFn: api.updateAISettings,
    onSuccess: (result) => {
      setPlanner(result.planner);
      setSearch(result.search);
      setEffort(result.planner_effort);
      setChanged(false);
    },
  });

  function changePlanner(choice: AISettingsChoice) {
    setPlanner(choice);
    setChanged(true);
    save.reset();
  }

  function changeSearch(choice: AISettingsChoice) {
    setSearch(choice);
    setChanged(true);
    save.reset();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate({ planner, search, planner_effort: effort });
  }

  return (
    <form className="grid gap-6" onSubmit={submit}>
      <section className="grid gap-5 border border-border bg-surface p-4 sm:p-5 lg:grid-cols-[220px_minmax(0,1fr)]">
        <div>
          <span className="mb-3 grid size-9 place-items-center border border-border-strong bg-inset text-accent">
            <Bot aria-hidden="true" className="size-4" />
          </span>
          <h2 className="font-semibold text-ui">Planner defaults</h2>
          <p className="mt-1 text-meta text-fg-muted">
            Used when a new execution graph does not provide an override.
          </p>
        </div>
        <div className="grid min-w-0 gap-5">
          <RuntimeField
            choice={planner}
            legend="Planner runtime"
            name="settings-planner-runtime"
            onChange={changePlanner}
            options={initial.planner_options}
          />
          <label className="grid min-w-0 gap-1.5 border-border border-t pt-4 text-meta text-fg-muted">
            Planner effort
            <select
              aria-label="Planner effort"
              className={`${CONTROL} font-mono text-code`}
              onChange={(event) => {
                setEffort(event.target.value as PlannerEffort);
                setChanged(true);
                save.reset();
              }}
              value={effort}
            >
              {EFFORTS.map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </select>
            <span className="text-badge text-fg-subtle">
              Applied by runtimes that support explicit effort controls.
            </span>
          </label>
        </div>
      </section>

      <section className="grid gap-5 border border-border bg-surface p-4 sm:p-5 lg:grid-cols-[220px_minmax(0,1fr)]">
        <div>
          <span className="mb-3 grid size-9 place-items-center border border-border-strong bg-inset text-accent">
            <SearchCode aria-hidden="true" className="size-4" />
          </span>
          <h2 className="font-semibold text-ui">Code Search runtime</h2>
          <p className="mt-1 text-meta text-fg-muted">
            Answers repository questions and validates source citations.
          </p>
        </div>
        <RuntimeField
          choice={search}
          legend="Code Search runtime"
          name="settings-search-runtime"
          onChange={changeSearch}
          options={initial.search_options}
        />
      </section>

      <footer className="sticky bottom-0 flex flex-wrap items-center justify-end gap-3 border border-border bg-elevated/95 p-3">
        {save.isSuccess && !changed ? (
          <p
            className="mr-auto flex items-center gap-1.5 text-done text-meta"
            role="status"
          >
            <CheckCircle2 aria-hidden="true" className="size-3.5" /> Settings
            saved.
          </p>
        ) : null}
        {save.error ? (
          <p className="mr-auto text-failed text-meta" role="alert">
            {save.error.message}
          </p>
        ) : null}
        <Button disabled={save.isPending || !changed} type="submit">
          {save.isPending ? "Saving…" : "Save settings"}
        </Button>
      </footer>
    </form>
  );
}

export function SettingsRoute() {
  const settings = useQuery({
    queryKey: ["ai-settings"],
    queryFn: api.getAISettings,
  });

  return (
    <div className="min-h-full bg-bg">
      <header className="border-border border-b bg-surface/40 px-4 py-4 sm:px-6">
        <p className="mb-1 flex items-center gap-1.5 font-mono text-badge uppercase tracking-[0.14em] text-accent">
          <Settings2 aria-hidden="true" className="size-3" /> Configuration
        </p>
        <h1 className="font-semibold text-title">AI runtime settings</h1>
        <p className="mt-1 max-w-2xl text-meta text-fg-muted">
          Set global defaults for planning and repository investigation.
        </p>
      </header>
      <main className="mx-auto w-full max-w-5xl p-4 sm:p-6">
        {settings.isLoading ? (
          <p className="text-meta text-fg-muted" role="status">
            Loading settings…
          </p>
        ) : settings.error ? (
          <p
            className="border border-failed/30 bg-failed/5 p-4 text-failed text-meta"
            role="alert"
          >
            {settings.error.message}
          </p>
        ) : settings.data ? (
          <SettingsForm initial={settings.data} />
        ) : null}
      </main>
    </div>
  );
}
