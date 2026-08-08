import { AlertTriangle, FileCode2, X } from "lucide-react";
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
      <header className="flex h-8 shrink-0 items-center gap-2 border-border border-b bg-surface px-3">
        <FileCode2 className="size-3.5 shrink-0 text-fg-muted" />
        <span className="min-w-0 flex-1 truncate font-mono text-code">
          {target
            ? `${target.path}:${target.line}-${target.endLine}`
            : "Source"}
        </span>
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
          <div className="flex h-full min-h-48 items-center justify-center p-4 text-center text-fg-subtle text-meta">
            Open a citation to inspect its exact line range.
          </div>
        ) : loading ? (
          <p className="p-3 text-fg-muted text-meta">Loading source…</p>
        ) : error ? (
          <div className="flex gap-2 p-3 text-failed text-meta" role="alert">
            <AlertTriangle className="size-3.5 shrink-0" />
            The cited file is unavailable or moved. This citation is stale.
          </div>
        ) : file ? (
          <section
            aria-label={`${target.path} lines ${target.line} through ${target.endLine}`}
            className="min-w-max py-2 font-mono text-code leading-relaxed"
          >
            {file.lines.map((line) => (
              <div className="flex bg-elevated" key={line.line}>
                <span
                  aria-hidden="true"
                  className="sticky left-0 w-14 shrink-0 select-none border-border border-r bg-surface pr-2 text-right text-fg-subtle"
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
