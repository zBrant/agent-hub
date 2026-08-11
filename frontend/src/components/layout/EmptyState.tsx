import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

type Props = {
  icon: LucideIcon;
  title: string;
  description: ReactNode;
};

/**
 * Honest "nothing here yet". No skeleton, no mock rows — placeholder data in an
 * operations console is indistinguishable from a stale reading.
 */
export function EmptyState({ icon: Icon, title, description }: Props) {
  return (
    <div className="flex h-full items-center justify-center p-6">
      <div className="flex max-w-sm flex-col items-center text-center">
        <div className="mb-4 grid size-11 place-items-center rounded-sm border border-border bg-surface shadow-[inset_0_1px_0_var(--color-border)]">
          <Icon
            aria-hidden="true"
            className="size-5 text-accent"
            strokeWidth={1.5}
          />
        </div>
        <span aria-hidden="true" className="mb-3 h-px w-8 bg-border-strong" />
        <h2 className="font-semibold text-ui tracking-tight">{title}</h2>
        <p className="mt-1.5 text-fg-muted text-meta leading-relaxed">
          {description}
        </p>
      </div>
    </div>
  );
}
