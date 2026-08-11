import { BadgeCheck, CreditCard } from "lucide-react";
import type { PlannerOption } from "@/api/client";
import { harnessDotClass } from "@/lib/harness";
import { plannerOptionKey } from "@/lib/planner-selection";
import { cn } from "@/lib/utils";

type Props = {
  active: boolean;
  onChange: () => void;
  option: PlannerOption;
};

export function PlannerOptionCard({ active, onChange, option }: Props) {
  const key = plannerOptionKey(option.backend, option.harness);
  return (
    <label
      className={cn(
        "relative flex min-h-14 cursor-pointer flex-col justify-center gap-1 bg-inset px-3 py-2 outline-none",
        active
          ? "bg-accent/8 shadow-[inset_2px_0_var(--color-accent)]"
          : "hover:bg-elevated",
      )}
    >
      <span className="flex items-center gap-2">
        <input
          checked={active}
          className="size-3.5 shrink-0 accent-accent"
          name="planner-backend"
          onChange={onChange}
          type="radio"
          value={key}
        />
        <span
          aria-hidden="true"
          className={cn(
            "size-1.5 shrink-0 rounded-full",
            harnessDotClass(option.harness),
          )}
        />
        {option.harness ? (
          <code className="truncate text-code text-fg">{option.harness}</code>
        ) : (
          <span className="truncate text-ui text-fg">Anthropic API</span>
        )}
      </span>
      <span
        className={cn(
          "flex items-center gap-1 text-badge",
          option.is_spend ? "text-review" : "text-fg-muted",
        )}
      >
        {option.is_spend ? (
          <CreditCard aria-hidden="true" className="size-3.5" />
        ) : (
          <BadgeCheck aria-hidden="true" className="size-3.5" />
        )}
        {option.is_spend ? "Billed per token" : "Subscription"}
      </span>
    </label>
  );
}
