import { ConnectionStatus } from "@/components/layout/ConnectionStatus";
import { TabNav } from "@/components/layout/TabNav";
import type { ConnectionStatus as Status } from "@/ws/client";

type Props = {
  status: Status;
  attempt: number;
  onReconnect: () => void;
};

/** Responsive application chrome: top navigation on mobile, activity rail on desktop. */
export function TopBar({ status, attempt, onReconnect }: Props) {
  return (
    <header className="relative z-20 flex h-[52px] shrink-0 items-center border-border border-b bg-surface px-3 md:h-full md:w-[84px] md:flex-col md:border-r md:border-b-0 md:px-2 md:py-3">
      <div className="flex shrink-0 items-center gap-2.5 md:flex-col md:gap-1.5">
        <span
          aria-hidden="true"
          className="relative grid size-7 place-items-center rounded-sm border border-border-strong bg-inset"
        >
          <span className="absolute top-[5px] size-1.5 rounded-full bg-accent" />
          <span className="absolute bottom-[5px] left-[6px] size-1.5 rounded-full bg-done" />
          <span className="absolute right-[6px] bottom-[5px] size-1.5 rounded-full bg-running" />
          <span className="h-2.5 w-px bg-border-strong" />
        </span>
        <span className="hidden font-semibold text-ui tracking-[-0.025em] sm:inline md:text-badge md:uppercase md:tracking-[0.08em]">
          AgentHub
        </span>
      </div>
      <TabNav />
      <div className="ml-auto border-border md:mt-auto md:ml-0 md:w-full md:border-t md:pt-3">
        <ConnectionStatus
          status={status}
          attempt={attempt}
          onReconnect={onReconnect}
        />
      </div>
    </header>
  );
}
