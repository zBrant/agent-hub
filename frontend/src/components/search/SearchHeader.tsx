import { Search } from "lucide-react";
import type { Session } from "@/api/client";

type Props = {
  sessions: readonly Session[];
  selectedId: string;
  loading: boolean;
  pending: boolean;
  onChange: (sessionId: string) => void;
};

export function SearchHeader({
  sessions,
  selectedId,
  loading,
  pending,
  onChange,
}: Props) {
  const selected = sessions.find((session) => session.id === selectedId);

  return (
    <header className="flex min-h-12 shrink-0 flex-wrap items-center gap-3 border-border border-b bg-surface px-4 py-2">
      <div className="flex min-w-40 items-center gap-2">
        <Search className="size-4 text-accent" />
        <div>
          <h1 className="font-semibold text-title">Code search</h1>
          <p className="text-badge text-fg-subtle">Evidence-backed answers</p>
        </div>
      </div>
      <label className="ml-auto flex min-w-0 items-center gap-2 text-fg-muted text-meta">
        Session
        <select
          className="h-[30px] max-w-80 rounded-md border border-border bg-inset px-2 text-fg text-ui outline-none focus:border-focus disabled:opacity-60"
          disabled={loading || pending}
          onChange={(event) => onChange(event.target.value)}
          value={selectedId}
        >
          {!sessions.length ? <option value="">No sessions</option> : null}
          {sessions.map((session) => (
            <option key={session.id} value={session.id}>
              {session.title}
            </option>
          ))}
        </select>
      </label>
      {selected ? (
        <code className="hidden max-w-80 truncate text-code text-fg-subtle xl:block">
          {selected.repo_path}
        </code>
      ) : null}
    </header>
  );
}
