import { FileDiff } from "lucide-react";
import { memo, useMemo, useState } from "react";

type Props = {
  patch: string;
};

function lineTone(line: string): string {
  if (line.startsWith("diff --git") || line.startsWith("@@")) {
    return "bg-accent/8 text-accent";
  }
  if (line.startsWith("+++") || line.startsWith("---")) {
    return "text-fg-muted";
  }
  if (line.startsWith("+")) return "bg-done/8 text-done";
  if (line.startsWith("-")) return "bg-failed/8 text-blocked";
  return "text-fg-muted";
}

type DiffLine = {
  key: number;
  line: string;
  oldLine: number | null;
  newLine: number | null;
};

const INITIAL_LINES = 250;
const LINE_STEP = 250;
const MAX_LINE_CHARS = 2_000;

function parseLines(lines: readonly string[]): DiffLine[] {
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  return lines.map((line, index) => {
    const hunk = /^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/u.exec(line);
    if (hunk) {
      oldLine = Number(hunk[1]);
      newLine = Number(hunk[2]);
      inHunk = true;
    }
    if (line.startsWith("diff --git")) inHunk = false;

    let shownOld: number | null = null;
    let shownNew: number | null = null;
    if (inHunk && !hunk && !line.startsWith("\\")) {
      if (line.startsWith("+")) {
        shownNew = newLine++;
      } else if (line.startsWith("-")) {
        shownOld = oldLine++;
      } else {
        shownOld = oldLine++;
        shownNew = newLine++;
      }
    }

    return {
      key: index,
      line,
      oldLine: shownOld,
      newLine: shownNew,
    };
  });
}

function visibleLine(line: string): string {
  if (line.length <= MAX_LINE_CHARS) return line || " ";
  return `${line.slice(0, MAX_LINE_CHARS)} … [${(
    line.length - MAX_LINE_CHARS
  ).toLocaleString("en")} characters omitted]`;
}

function lineCount(patch: string): number {
  if (!patch) return 0;
  let count = 1;
  for (let index = 0; index < patch.length; index += 1) {
    if (patch.charCodeAt(index) === 10) count += 1;
  }
  return count;
}

function firstLines(patch: string, limit: number): string[] {
  const lines: string[] = [];
  let start = 0;
  while (lines.length < limit && start <= patch.length) {
    const end = patch.indexOf("\n", start);
    if (end === -1) {
      lines.push(patch.slice(start));
      break;
    }
    lines.push(patch.slice(start, end));
    start = end + 1;
  }
  return lines;
}

export const DiffView = memo(function DiffView({ patch }: Props) {
  const totalLines = useMemo(() => lineCount(patch), [patch]);
  const [renderWindow, setRenderWindow] = useState({
    patch,
    count: INITIAL_LINES,
  });
  const visibleCount =
    renderWindow.patch === patch
      ? renderWindow.count
      : Math.min(INITIAL_LINES, totalLines);
  const lines = useMemo(
    () => parseLines(firstLines(patch, visibleCount)),
    [patch, visibleCount],
  );
  const remaining = totalLines - lines.length;

  function showMore() {
    setRenderWindow((current) => ({
      patch,
      count:
        current.patch === patch
          ? Math.min(totalLines, current.count + LINE_STEP)
          : Math.min(totalLines, INITIAL_LINES + LINE_STEP),
    }));
  }

  return (
    <section
      aria-labelledby="diff-heading"
      className="min-h-0 flex-1 overflow-auto bg-inset"
    >
      <div className="sticky top-0 z-10 flex h-9 items-center gap-2 border-border border-b bg-surface px-3">
        <FileDiff className="size-3.5 text-fg-muted" />
        <h2 id="diff-heading" className="font-semibold text-ui">
          Final diff
        </h2>
        <span className="ml-auto font-mono text-badge text-fg-subtle">
          {lines.length.toLocaleString("en")} /{" "}
          {totalLines.toLocaleString("en")} lines
        </span>
      </div>
      {patch ? (
        <div className="min-w-max py-2 font-mono text-code leading-relaxed">
          {lines.map(({ key, line, newLine, oldLine }) => (
            <div
              className={`grid grid-cols-[42px_42px_minmax(0,1fr)] ${lineTone(line)}`}
              key={key}
            >
              <span
                aria-hidden="true"
                className="select-none border-border/70 border-r px-1.5 text-right text-fg-subtle"
              >
                {oldLine ?? ""}
              </span>
              <span
                aria-hidden="true"
                className="select-none border-border/70 border-r px-1.5 text-right text-fg-subtle"
              >
                {newLine ?? ""}
              </span>
              <code className="whitespace-pre px-3">{visibleLine(line)}</code>
            </div>
          ))}
          {remaining > 0 ? (
            <div className="sticky left-0 flex w-screen max-w-full justify-center border-border border-t bg-surface p-3">
              <button
                className="border border-border-strong bg-elevated px-3 py-1.5 font-sans text-meta text-fg hover:border-accent hover:text-accent"
                onClick={showMore}
                type="button"
              >
                Show next {Math.min(LINE_STEP, remaining).toLocaleString("en")}{" "}
                lines
              </button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className="flex min-h-32 items-center justify-center p-4 text-center">
          <div>
            <FileDiff className="mx-auto mb-2 size-5 text-fg-subtle" />
            <p className="text-meta text-fg-muted">No worktree changes yet.</p>
          </div>
        </div>
      )}
    </section>
  );
});
