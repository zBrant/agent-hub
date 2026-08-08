import { Check, RotateCcw } from "lucide-react";
import { useState } from "react";
import type { AcceptanceResult, CriterionOutcome } from "@/api/client";
import { AcceptanceChecklist } from "@/components/graph/AcceptanceChecklist";
import { DiffView } from "@/components/session/DiffView";
import { Button } from "@/components/ui/button";

type Props = {
  acceptance: readonly AcceptanceResult[];
  patch: string;
  busy: boolean;
  onApprove: (outcomes: Readonly<Record<number, CriterionOutcome>>) => void;
  onReject: (
    feedback: string,
    outcomes: Readonly<Record<number, CriterionOutcome>>,
  ) => void;
};

export function NodeReviewPanel({
  acceptance,
  patch,
  busy,
  onApprove,
  onReject,
}: Props) {
  const [feedback, setFeedback] = useState("");
  const [outcomes, setOutcomes] = useState<
    Readonly<Record<number, CriterionOutcome>>
  >({});

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <AcceptanceChecklist
        disabled={busy}
        onChange={(position, outcome) =>
          setOutcomes((current) => ({ ...current, [position]: outcome }))
        }
        outcomes={outcomes}
        results={acceptance}
      />
      <div className="min-h-0 flex-1">
        <DiffView patch={patch} />
      </div>
      <div className="space-y-2 border-border border-t bg-surface p-3">
        <label className="block text-meta text-fg-muted">
          Rejection feedback
          <textarea
            aria-label="Rejection feedback"
            className="mt-1 min-h-24 w-full resize-y rounded-md border border-border-strong bg-inset p-2 text-ui text-fg"
            disabled={busy}
            onChange={(event) => setFeedback(event.target.value)}
            placeholder="Explain what the next attempt must change"
            value={feedback}
          />
        </label>
        <div className="flex items-center justify-between gap-2">
          <Button
            disabled={busy || !feedback.trim()}
            onClick={() => onReject(feedback.trim(), outcomes)}
            size="sm"
            variant="destructive"
          >
            <RotateCcw /> Reject and retry
          </Button>
          <Button disabled={busy} onClick={() => onApprove(outcomes)} size="sm">
            <Check /> Approve merge
          </Button>
        </div>
      </div>
    </div>
  );
}
