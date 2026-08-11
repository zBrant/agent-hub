import { FileDiff } from "lucide-react";

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
  key: string;
  line: string;
  oldLine: number | null;
  newLine: number | null;
};

function parseLines(patch: string): DiffLine[] {
  const occurrences = new Map<string, number>();
  let oldLine = 0;
  let newLine = 0;
  let inHunk = false;
  return patch.split("\n").map((line) => {
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

    const occurrence = (occurrences.get(line) ?? 0) + 1;
    occurrences.set(line, occurrence);
    return {
      key: `${line}:${occurrence}`,
      line,
      oldLine: shownOld,
      newLine: shownNew,
    };
  });
}

export function DiffView({ patch }: Props) {
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
          unified patch
        </span>
      </div>
      {patch ? (
        <div className="min-w-max py-2 font-mono text-code leading-relaxed">
          {parseLines(patch).map(({ key, line, newLine, oldLine }) => (
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
              <code className="whitespace-pre px-3">{line || " "}</code>
            </div>
          ))}
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
}
