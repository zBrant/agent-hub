import { ConnectionStatus } from "@/components/layout/ConnectionStatus";
import { TabNav } from "@/components/layout/TabNav";
import type { ConnectionStatus as Status } from "@/ws/client";

type Props = {
  status: Status;
  attempt: number;
  onReconnect: () => void;
};

/** 44px application bar — design-system §4. */
export function TopBar({ status, attempt, onReconnect }: Props) {
  return (
    <header className="flex h-[44px] shrink-0 items-center gap-4 border-border border-b bg-surface px-4">
      <span className="font-semibold text-ui tracking-tight">AgentHub</span>
      <TabNav />
      <div className="ml-auto">
        <ConnectionStatus
          status={status}
          attempt={attempt}
          onReconnect={onReconnect}
        />
      </div>
    </header>
  );
}
