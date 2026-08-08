import type { AcceptanceResult, CriterionOutcome } from "@/api/client";

type Props = {
  results: readonly AcceptanceResult[];
  outcomes: Readonly<Record<number, CriterionOutcome>>;
  disabled: boolean;
  onChange: (position: number, outcome: CriterionOutcome) => void;
};

export function AcceptanceChecklist({
  results,
  outcomes,
  disabled,
  onChange,
}: Props) {
  return (
    <section className="border-border border-b">
      <div className="flex h-8 items-center justify-between px-3">
        <h3 className="font-semibold text-ui">Acceptance criteria</h3>
        <span className="text-meta text-fg-muted">
          {results.length} criteria
        </span>
      </div>
      {results.length === 0 ? (
        <p className="border-border border-t p-3 text-meta text-fg-muted">
          No acceptance criteria were recorded for this attempt.
        </p>
      ) : (
        <ol className="divide-y divide-border border-border border-t">
          {results.map((result) => (
            <li
              className="grid grid-cols-[1fr_112px] gap-3 p-3"
              key={result.position}
            >
              <span className="text-ui">{result.criterion}</span>
              <select
                aria-label={`Outcome for ${result.criterion}`}
                className="h-[30px] rounded-md border border-border-strong bg-inset px-2 text-meta text-fg"
                disabled={disabled}
                onChange={(event) =>
                  onChange(
                    result.position,
                    event.target.value as CriterionOutcome,
                  )
                }
                value={outcomes[result.position] ?? result.outcome}
              >
                <option value="unevaluated">Unevaluated</option>
                <option value="pass">Pass</option>
                <option value="fail">Fail</option>
              </select>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
