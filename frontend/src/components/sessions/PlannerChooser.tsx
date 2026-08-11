import { BadgeCheck, CreditCard, TriangleAlert } from "lucide-react";
import type { PlannerOption, PlannerOptions } from "@/api/client";
import { PlannerOptionCard } from "@/components/sessions/PlannerOptionCard";
import {
  CLI_DEFAULT_MODEL,
  findPlannerOption,
  type PlannerSelection,
  plannerBackendLabel,
  plannerOptionKey,
} from "@/lib/planner-selection";
import { cn } from "@/lib/utils";

type Props = {
  data: PlannerOptions | undefined;
  error: Error | null;
  isLoading: boolean;
  selection: PlannerSelection | null;
  onModelChange: (model: string) => void;
  onOptionChange: (option: PlannerOption) => void;
};

const CONTROL =
  "h-[30px] rounded-md border border-border bg-inset px-2 text-ui text-fg outline-none focus:border-focus";

export function PlannerChooser({
  data,
  error,
  isLoading,
  selection,
  onModelChange,
  onOptionChange,
}: Props) {
  const chosen =
    data && selection
      ? findPlannerOption(data.options, selection.backend, selection.harness)
      : null;

  return (
    <fieldset>
      <legend className="mb-3 text-badge font-medium uppercase tracking-[0.12em] text-fg-muted">
        Planner runtime
      </legend>

      {isLoading ? (
        <p className="text-meta text-fg-muted">Loading planner options…</p>
      ) : error ? (
        <p className="text-meta text-review" role="status">
          Planner options unavailable ({error.message}). This plan will use the
          server's configured default planner.
        </p>
      ) : data ? (
        <>
          {data.default.selectable ? null : (
            <p
              className="mb-2 flex items-start gap-1.5 text-meta text-review"
              role="alert"
            >
              <TriangleAlert aria-hidden="true" className="size-3.5" />
              <span>
                The server's configured default planner (
                <code className="text-code">
                  {plannerBackendLabel(data.default)}
                </code>
                ) cannot back a plan, so nothing is preselected. Choose a
                planner below.
              </span>
            </p>
          )}

          <div className="grid grid-cols-1 gap-px overflow-hidden border border-border bg-border">
            {data.options.map((option) => {
              const active =
                selection !== null &&
                selection.backend === option.backend &&
                selection.harness === option.harness;
              return (
                <PlannerOptionCard
                  active={active}
                  key={plannerOptionKey(option.backend, option.harness)}
                  onChange={() => onOptionChange(option)}
                  option={option}
                />
              );
            })}
          </div>

          {chosen && selection ? (
            <div className="mt-4 grid min-w-0 grid-cols-1 gap-3 border-border border-t pt-4">
              <label className="grid min-w-0 gap-1 text-meta text-fg-muted">
                Planner model
                <select
                  className={cn(
                    CONTROL,
                    "min-w-0 w-full max-w-full font-mono text-code",
                  )}
                  onChange={(event) => onModelChange(event.target.value)}
                  value={selection.model ?? CLI_DEFAULT_MODEL}
                >
                  {chosen.backend === "harness" ? (
                    <option value={CLI_DEFAULT_MODEL}>
                      {plannerBackendLabel(chosen)} default — whatever the CLI
                      is configured for
                    </option>
                  ) : null}
                  {chosen.models.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </label>
              <div className="grid min-w-0 gap-1">
                <p
                  className={cn(
                    "flex items-start gap-1.5 text-meta",
                    chosen.is_spend ? "text-review" : "text-fg-muted",
                  )}
                >
                  {chosen.is_spend ? (
                    <CreditCard
                      aria-hidden="true"
                      className="size-3.5 shrink-0"
                    />
                  ) : (
                    <BadgeCheck
                      aria-hidden="true"
                      className="size-3.5 shrink-0"
                    />
                  )}
                  <span className="min-w-0 break-words">
                    {chosen.is_spend
                      ? "Billed per token against your Anthropic API key: planning this objective is real spend."
                      : `Planning runs on the ${plannerBackendLabel(chosen)} subscription you already pay for. Any cost reported afterwards is an estimated equivalent, not new spend.`}
                  </span>
                </p>
                {chosen.supports_effort ? null : (
                  <p className="text-meta text-fg-subtle">
                    Planner effort is an Anthropic API setting;{" "}
                    <code className="text-code">
                      {plannerBackendLabel(chosen)}
                    </code>{" "}
                    decides its own planning depth.
                  </p>
                )}
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </fieldset>
  );
}
