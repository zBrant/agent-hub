import {
  AlertTriangle,
  Braces,
  Check,
  FileCode2,
  MousePointer2,
  X,
} from "lucide-react";
import type { ReactNode } from "react";
import type { FileRead } from "@/api/client";

export type CitationTarget = {
  path: string;
  line: number;
  endLine: number;
  expectedHash: string | null;
};

type Props = {
  target: CitationTarget | null;
  file: FileRead | undefined;
  loading: boolean;
  error: Error | null;
  onClose: () => void;
};

const tokenPattern =
  /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\/\/.*$|#.*$|\b(?:async|await|break|case|catch|class|const|continue|def|do|else|enum|export|extends|false|finally|for|from|function|if|import|in|interface|let|match|new|none|null|pass|private|protected|public|raise|return|self|static|super|switch|this|throw|true|try|type|typeof|undefined|var|while|with|yield)\b|\b\d+(?:\.\d+)?\b)/giu;

export function SearchCodePanel({
  target,
  file,
  loading,
  error,
  onClose,
}: Props) {
  const stale = Boolean(
    target?.expectedHash && file && target.expectedHash !== file.content_hash,
  );

  return (
    <aside
      aria-label="Citation source"
      className="flex h-full min-h-0 flex-col bg-inset"
    >
      <header className="flex min-h-12 shrink-0 items-center gap-3 border-border border-b bg-surface px-3 py-2">
        <FileCode2 className="size-4 shrink-0 text-accent" />
        <div className="min-w-0 flex-1">
          <p className="text-badge text-fg-subtle uppercase tracking-[0.12em]">
            Evidence source
          </p>
          <p className="truncate font-mono text-code text-fg">
            {target ? target.path : "No source selected"}
          </p>
        </div>
        {target ? (
          <span className="border border-border bg-inset px-1.5 py-0.5 font-mono text-badge text-fg-muted">
            L{target.line}–{target.endLine}
          </span>
        ) : null}
        {target ? (
          <button
            aria-label="Close source panel"
            className="text-fg-muted hover:text-fg"
            onClick={onClose}
            type="button"
          >
            <X className="size-4" />
          </button>
        ) : null}
      </header>

      {stale ? (
        <div
          className="flex items-start gap-2 border-review border-b bg-surface px-3 py-2 text-review text-meta"
          role="alert"
        >
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          This citation is stale. The cited lines changed after the answer was
          produced; the current content is shown below.
        </div>
      ) : null}

      <div className="min-h-0 flex-1 overflow-auto">
        {!target ? (
          <div className="flex h-full min-h-48 items-center justify-center p-6">
            <div className="max-w-72 border-border border-l pl-4">
              <MousePointer2 className="mb-3 size-5 text-accent" />
              <p className="font-semibold text-ui text-fg">
                Inspect supporting code
              </p>
              <p className="mt-1 text-fg-muted text-meta leading-relaxed">
                Select a file reference in the investigation report to open the
                exact lines used as evidence.
              </p>
              <div className="mt-4 grid gap-2 text-badge text-fg-subtle">
                <p className="flex items-center gap-2">
                  <Braces className="size-3" /> Syntax-aware preview
                </p>
                <p className="flex items-center gap-2">
                  <Check className="size-3" /> Content hash validation
                </p>
              </div>
            </div>
          </div>
        ) : loading ? (
          <div aria-live="polite" className="p-4 text-fg-muted text-meta">
            Reading evidence snapshot…
          </div>
        ) : error ? (
          <div
            className="m-3 flex gap-2 border border-failed bg-surface p-3 text-failed text-meta"
            role="alert"
          >
            <AlertTriangle className="size-3.5 shrink-0" />
            The cited file is unavailable or moved. This citation is stale.
          </div>
        ) : file ? (
          <section
            aria-label={`${target.path} lines ${target.line} through ${target.endLine}`}
            className="min-w-max py-3 font-mono text-code leading-relaxed"
          >
            {file.lines.map((line) => (
              <div
                className="flex border-accent border-l-2 bg-surface"
                key={line.line}
              >
                <span
                  aria-hidden="true"
                  className="sticky left-0 w-14 shrink-0 select-none border-border border-r bg-inset pr-2 text-right text-fg-subtle"
                >
                  {line.line}
                </span>
                <code className="whitespace-pre px-3 text-fg">
                  {highlight(line.text)}
                </code>
              </div>
            ))}
          </section>
        ) : null}
      </div>
      {target && file ? (
        <footer className="flex h-7 shrink-0 items-center gap-2 border-border border-t bg-surface px-3 text-badge text-fg-subtle">
          {stale ? (
            <AlertTriangle className="size-3 text-review" />
          ) : (
            <Check className="size-3 text-done" />
          )}
          {stale
            ? "Current file differs from cited snapshot"
            : "Snapshot verified"}
          {file.truncated ? (
            <span className="ml-auto">Range truncated</span>
          ) : null}
        </footer>
      ) : null}
    </aside>
  );
}

function highlight(source: string): ReactNode {
  const parts: ReactNode[] = [];
  let cursor = 0;
  for (const match of source.matchAll(tokenPattern)) {
    const index = match.index;
    if (index > cursor) parts.push(source.slice(cursor, index));
    const token = match[0];
    parts.push(
      <span className={tokenClass(token)} key={`${index}:${token}`}>
        {token}
      </span>,
    );
    cursor = index + token.length;
  }
  if (cursor < source.length) parts.push(source.slice(cursor));
  return parts;
}

function tokenClass(token: string): string {
  if (token.startsWith("//") || token.startsWith("#")) {
    return "text-syntax-comment";
  }
  if (`"'\``.includes(token[0] ?? "")) return "text-syntax-string";
  if (/^\d/u.test(token)) return "text-syntax-number";
  return "text-syntax-keyword";
}
