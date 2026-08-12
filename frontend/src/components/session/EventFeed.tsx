import {
  Bot,
  Brain,
  CheckCircle2,
  CircleDot,
  Coins,
  ShieldAlert,
  TerminalSquare,
  Wrench,
  XCircle,
} from "lucide-react";
import type { AgentEvent } from "@/api/events";
import { cn } from "@/lib/utils";

type Props = {
  events: readonly AgentEvent[];
};

const MAX_VISIBLE = 300;

function keyedEvents(events: readonly AgentEvent[]) {
  const occurrences = new Map<string, number>();
  return events.map((event) => {
    const fact = JSON.stringify(event);
    const occurrence = (occurrences.get(fact) ?? 0) + 1;
    occurrences.set(fact, occurrence);
    return { event, key: `${fact}:${occurrence}` };
  });
}

function time(ts: number): string {
  return new Date(ts).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function EventRow({ event }: { event: AgentEvent }) {
  const base =
    "group grid grid-cols-[24px_68px_minmax(0,1fr)] gap-2 border-border/70 border-b px-3 py-2.5 hover:bg-elevated/45 [&>svg]:rounded-sm [&>svg]:bg-inset [&>svg]:p-1 [&>svg]:size-6";
  switch (event.type) {
    case "assistant_text":
      return (
        <div className={base}>
          <Bot className="size-4 text-accent" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <p className="whitespace-pre-wrap text-ui leading-relaxed">
            {event.text}
          </p>
        </div>
      );
    case "thinking_delta":
      return (
        <div className={base}>
          <Brain className="size-4 text-fg-subtle" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <p className="whitespace-pre-wrap text-meta text-fg-muted italic">
            {event.text}
          </p>
        </div>
      );
    case "tool_call":
      return (
        <div className={base}>
          <Wrench className="size-4 text-review" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <div>
            <span className="font-mono text-code">{event.tool}</span>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-code text-fg-muted">
              {JSON.stringify(event.input ?? {}, null, 2)}
            </pre>
          </div>
        </div>
      );
    case "tool_result":
      return (
        <div className={base}>
          {event.ok ? (
            <CheckCircle2 className="size-4 text-done" />
          ) : (
            <XCircle className="size-4 text-failed" />
          )}
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <div
            className={cn(
              "font-mono text-code",
              event.denied ? "text-blocked" : "text-fg-muted",
            )}
          >
            {event.denied ? "Denied · " : ""}
            {event.preview || (event.ok ? "Tool completed" : "Tool failed")}
          </div>
        </div>
      );
    case "usage": {
      const total =
        (event.input_tokens ?? 0) +
        (event.output_tokens ?? 0) +
        (event.cache_read_tokens ?? 0) +
        (event.cache_write_tokens ?? 0);
      return (
        <div className={base}>
          <Coins className="size-4 text-fg-muted" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <span className="text-meta text-fg-muted">
            {total.toLocaleString()} tokens · {event.model}
            {event.source === "reconstructed" ? " · reconstructed" : ""}
          </span>
        </div>
      );
    }
    case "permission":
      return (
        <div className={base}>
          <ShieldAlert className="size-4 text-blocked" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <span>{event.description}</span>
        </div>
      );
    case "run_started":
      return (
        <div className={base}>
          <CircleDot className="size-4 text-running" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <span>
            Run started ·{" "}
            <code className="text-code">
              {event.harness} / {event.model}
            </code>
          </span>
        </div>
      );
    case "run_finished":
      return (
        <div className={base}>
          {event.status === "success" ? (
            <CheckCircle2 className="size-4 text-done" />
          ) : (
            <XCircle className="size-4 text-failed" />
          )}
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <span>
            Run finished · <strong>{event.status}</strong>
            {event.summary ? ` · ${event.summary}` : ""}
          </span>
        </div>
      );
    case "turn_started":
    case "turn_finished":
      return (
        <div className={base}>
          <TerminalSquare className="size-4 text-fg-subtle" />
          <time className="font-mono text-code text-fg-subtle">
            {time(event.ts)}
          </time>
          <span className="text-meta text-fg-muted">
            Turn {event.turn} {"status" in event ? event.status : "started"}
          </span>
        </div>
      );
    case "raw_chunk":
      return null;
    default:
      return null;
  }
}

export function EventFeed({ events }: Props) {
  const hidden = Math.max(0, events.length - MAX_VISIBLE);
  const visible = keyedEvents(events.slice(hidden)).reverse();
  return (
    <section
      aria-labelledby="feed-heading"
      className="min-h-0 max-h-full flex-1 overflow-y-auto overscroll-contain bg-surface"
      aria-live="polite"
    >
      <div className="sticky top-0 z-10 flex h-9 items-center justify-between border-border border-b bg-surface px-3">
        <h2 id="feed-heading" className="font-semibold text-ui">
          Event feed
        </h2>
        <div className="flex items-center gap-2 font-mono text-badge text-fg-muted">
          <span>Latest first</span>
          <span aria-hidden="true">·</span>
          <span>{events.length} events</span>
        </div>
      </div>
      {visible.length === 0 ? (
        <p className="p-3 text-meta text-fg-muted">
          No events persisted for this run yet.
        </p>
      ) : (
        visible.map(({ event, key }) => <EventRow key={key} event={event} />)
      )}
      {hidden > 0 ? (
        <p className="border-border border-t px-3 py-2 text-meta text-fg-muted">
          {hidden} older events retained in state but hidden for rendering
          performance.
        </p>
      ) : null}
    </section>
  );
}
