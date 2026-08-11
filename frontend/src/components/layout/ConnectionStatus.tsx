import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ConnectionStatus as Status } from "@/ws/client";

type Props = {
  status: Status;
  attempt: number;
  onReconnect: () => void;
};

/**
 * State is colour + label here too (design-system §1.3): the dot never carries
 * the meaning on its own.
 */
const VISUAL: Record<Status, { label: string; dot: string; detail: string }> = {
  idle: {
    label: "Idle",
    dot: "bg-pending",
    detail: "The event stream has not been opened yet.",
  },
  connecting: {
    label: "Connecting",
    dot: "bg-running",
    detail: "Opening the event stream on /ws.",
  },
  open: {
    label: "Live",
    dot: "bg-done",
    detail: "Receiving events from the orchestrator on /ws.",
  },
  reconnecting: {
    label: "Reconnecting",
    dot: "bg-review",
    detail: "The event stream dropped. Retrying with backoff.",
  },
  closed: {
    label: "Offline",
    dot: "bg-failed",
    detail: "The event stream is closed.",
  },
};

export function ConnectionStatus({ status, attempt, onReconnect }: Props) {
  const visual = VISUAL[status];
  const offline = status === "reconnecting" || status === "closed";

  return (
    <div className="flex items-center gap-2 md:flex-col md:gap-1.5">
      <Tooltip>
        <TooltipTrigger
          aria-label={`Event stream: ${visual.label}`}
          className="flex cursor-default items-center gap-1.5 rounded-sm px-1 text-meta text-fg-muted md:flex-col md:gap-1 md:text-badge"
        >
          <span
            aria-hidden="true"
            className={cn(
              "size-1.5 rounded-full",
              visual.dot,
              status === "connecting" || status === "reconnecting"
                ? "animate-pulse"
                : null,
            )}
          />
          {visual.label}
        </TooltipTrigger>
        <TooltipContent>
          {visual.detail}
          {attempt > 0 ? ` Attempt ${attempt}.` : ""}
        </TooltipContent>
      </Tooltip>

      {offline ? (
        <Button
          aria-label="Retry event stream connection now"
          className="h-7 px-2 text-badge md:w-full md:px-1"
          size="sm"
          variant="outline"
          onClick={onReconnect}
        >
          <span className="md:hidden">Retry now</span>
          <span className="hidden md:inline">Retry</span>
        </Button>
      ) : null}
    </div>
  );
}
