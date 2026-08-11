import { Dialog } from "@base-ui/react/dialog";
import { ChartNoAxesColumnIncreasing, X } from "lucide-react";
import type { DashboardPeriod, MetricUsage } from "@/api/client";
import { TokenBreakdown } from "@/components/dashboard/TokenBreakdown";

type Props = {
  byHarness: readonly MetricUsage[];
  byModel: readonly MetricUsage[];
  period: DashboardPeriod;
  totalTokens: number;
};

const number = new Intl.NumberFormat("en");

const periodLabel: Record<DashboardPeriod, string> = {
  today: "Today",
  "7d": "Last 7 days",
  "30d": "Last 30 days",
};

export function TokenDistributionDialog({
  byHarness,
  byModel,
  period,
  totalTokens,
}: Props) {
  return (
    <Dialog.Root>
      <Dialog.Trigger className="mt-2 inline-flex items-center gap-1.5 border-border border-b pb-0.5 text-badge text-fg-muted hover:border-accent hover:text-fg">
        Inspect distribution
        <ChartNoAxesColumnIncreasing className="size-3" />
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Backdrop className="fixed inset-0 z-40 bg-bg/80 backdrop-blur-[2px] transition-opacity duration-150 data-ending-style:opacity-0 data-starting-style:opacity-0" />
        <Dialog.Popup className="fixed top-1/2 left-1/2 z-50 flex max-h-[min(760px,calc(100dvh-32px))] w-[min(920px,calc(100vw-32px))] -translate-x-1/2 -translate-y-1/2 flex-col border border-border-strong bg-elevated shadow-2xl transition-[scale,opacity] duration-150 data-ending-style:scale-[0.98] data-ending-style:opacity-0 data-starting-style:scale-[0.98] data-starting-style:opacity-0">
          <header className="flex shrink-0 items-start gap-3 border-border border-b bg-surface px-4 py-3">
            <span className="grid size-8 place-items-center border border-border bg-inset text-accent">
              <ChartNoAxesColumnIncreasing className="size-4" />
            </span>
            <div className="min-w-0 flex-1">
              <Dialog.Title className="font-semibold text-ui">
                Token distribution
              </Dialog.Title>
              <Dialog.Description className="mt-0.5 text-meta text-fg-muted">
                {number.format(totalTokens)} tokens · {periodLabel[period]} ·
                four-field accounting
              </Dialog.Description>
            </div>
            <Dialog.Close
              aria-label="Close token distribution"
              className="grid size-7 place-items-center text-fg-muted hover:bg-inset hover:text-fg"
            >
              <X className="size-4" />
            </Dialog.Close>
          </header>

          <div className="min-h-0 overflow-y-auto p-4">
            <div className="grid border border-border bg-surface lg:grid-cols-2 lg:divide-x lg:divide-border">
              <TokenBreakdown rows={byHarness} title="By harness" />
              <TokenBreakdown rows={byModel} title="By model" />
            </div>
            <div className="mt-3 grid grid-cols-2 gap-px border border-border bg-border sm:grid-cols-4">
              <TokenFact label="Cache read" detail="Reused context" />
              <TokenFact label="Cache write" detail="New cached context" />
              <TokenFact label="Input" detail="Uncached prompt" />
              <TokenFact label="Output" detail="Generated response" />
            </div>
          </div>
        </Dialog.Popup>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

function TokenFact({ label, detail }: { label: string; detail: string }) {
  return (
    <div className="bg-inset px-3 py-2">
      <p className="text-badge text-fg">{label}</p>
      <p className="mt-0.5 text-badge text-fg-subtle">{detail}</p>
    </div>
  );
}
