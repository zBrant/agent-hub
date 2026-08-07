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
    <div className="flex h-full items-center justify-center p-4">
      <div className="flex max-w-md flex-col items-center gap-3 text-center">
        <Icon aria-hidden="true" className="size-6 text-fg-subtle" />
        <h2 className="font-semibold text-ui">{title}</h2>
        <p className="text-fg-muted text-meta leading-relaxed">{description}</p>
      </div>
    </div>
  );
}
