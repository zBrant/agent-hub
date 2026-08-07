type Props = {
  patch: string;
};

export function DiffView({ patch }: Props) {
  return (
    <section
      aria-labelledby="diff-heading"
      className="min-h-0 flex-1 overflow-auto bg-inset"
    >
      <div className="sticky top-0 flex h-8 items-center border-border border-b bg-surface px-3">
        <h2 id="diff-heading" className="font-semibold text-ui">
          Final diff
        </h2>
      </div>
      {patch ? (
        <pre className="overflow-x-auto p-3 text-code leading-relaxed text-fg">
          {patch}
        </pre>
      ) : (
        <p className="p-3 text-meta text-fg-muted">No worktree changes yet.</p>
      )}
    </section>
  );
}
