import type {
  PlannerChoice,
  PlannerOption,
  PlannerOptions,
} from "@/api/client";

/** The generated request type, narrowed to a complete form selection. */
export type PlannerSelection = Required<Omit<PlannerChoice, "backend">> & {
  readonly backend: NonNullable<PlannerChoice["backend"]>;
};

/** `<select>`'s representation of the harness CLI's configured model. */
export const CLI_DEFAULT_MODEL = "";

export function plannerOptionKey(
  backend: PlannerSelection["backend"],
  harness: string | null,
) {
  return `${backend}:${harness ?? ""}`;
}

/** The API backend runs no harness, so it has no identifier to print. */
export function plannerBackendLabel(
  option: PlannerOption | PlannerOptions["default"],
) {
  return option.harness ?? "Anthropic API";
}

export function findPlannerOption(
  options: readonly PlannerOption[],
  backend: PlannerSelection["backend"],
  harness: string | null,
): PlannerOption | null {
  return (
    options.find(
      (option) => option.backend === backend && option.harness === harness,
    ) ?? null
  );
}

function initialModel(option: PlannerOption): string | null {
  if (option.backend === "harness") return null;
  return option.models[0] ?? null;
}

/** A model never follows a selection onto a backend that does not offer it. */
function reconcileModel(
  option: PlannerOption,
  model: string | null,
): string | null {
  if (model !== null && option.models.includes(model)) return model;
  return initialModel(option);
}

/** Resolve the configured default after the options query, unless the user picked. */
export function resolvePlannerSelection(
  data: PlannerOptions | undefined,
  picked: PlannerSelection | null,
): PlannerSelection | null {
  if (!data) return null;
  if (
    picked &&
    findPlannerOption(data.options, picked.backend, picked.harness)
  ) {
    return picked;
  }
  if (!data.default.selectable) return null;
  const option = findPlannerOption(
    data.options,
    data.default.backend,
    data.default.harness,
  );
  if (!option) return null;
  return {
    backend: option.backend,
    harness: option.harness,
    model: reconcileModel(option, data.default.model),
  };
}

export function selectPlannerOption(
  option: PlannerOption,
  previous: PlannerSelection | null,
): PlannerSelection {
  return {
    backend: option.backend,
    harness: option.harness,
    model: reconcileModel(option, previous?.model ?? null),
  };
}

export function selectPlannerModel(
  selection: PlannerSelection,
  model: string,
): PlannerSelection {
  return {
    ...selection,
    model: model === CLI_DEFAULT_MODEL ? null : model,
  };
}

/** Omit the wire field when the selection is exactly the server default. */
export function plannerRequestChoice(
  data: PlannerOptions | undefined,
  selection: PlannerSelection | null,
): PlannerChoice | null {
  if (!data || !selection) return null;
  const fallback = data.default;
  if (
    fallback.selectable &&
    fallback.backend === selection.backend &&
    fallback.harness === selection.harness &&
    fallback.model === selection.model
  ) {
    return null;
  }
  return selection;
}
