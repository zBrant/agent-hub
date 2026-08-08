import { Link } from "react-router";
import type { Dashboard } from "@/api/client";
import { nodeStateVisual } from "@/lib/node-state";

type Props = {
  events: Dashboard["event_feed"];
};

export function DashboardEventFeed({ events }: Props) {
  return (
    <section aria-labelledby="event-feed-heading">
      <h2 className="mb-2 font-semibold text-ui" id="event-feed-heading">
        Recent graph activity
      </h2>
      {events.length ? (
        <div className="overflow-hidden rounded-lg border border-border bg-surface">
          {events.map((event) => {
            const visual = nodeStateVisual(event.status);
            const Icon = visual.icon;
            return (
              <Link
                className="grid min-h-11 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-2 border-border border-b px-3 py-1.5 last:border-b-0 hover:bg-elevated"
                key={event.id}
                to={`/sessions/${encodeURIComponent(event.session_id)}?node=${encodeURIComponent(event.node_id)}`}
              >
                <Icon className={`size-4 ${visual.text}`} />
                <span className="min-w-0">
                  <span className="block truncate text-ui">
                    {event.node_name}
                  </span>
                  <span className="block truncate text-meta text-fg-subtle">
                    {event.session_title}
                  </span>
                </span>
                <span className="text-right">
                  <span className={`block text-meta ${visual.text}`}>
                    {visual.label}
                  </span>
                  <time
                    className="block font-mono text-badge text-fg-subtle"
                    dateTime={new Date(event.ts).toISOString()}
                  >
                    {new Date(event.ts).toLocaleTimeString()}
                  </time>
                </span>
              </Link>
            );
          })}
        </div>
      ) : (
        <div className="rounded-lg border border-border bg-surface p-3 text-meta text-fg-muted">
          No completed, failed, or operator-blocked node transitions yet.
        </div>
      )}
    </section>
  );
}
