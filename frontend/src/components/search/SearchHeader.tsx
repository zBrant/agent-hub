import { FileSearch2, GitBranch, LockKeyhole } from "lucide-react";
import type { SearchBranch, SearchProject } from "@/api/client";

type Props = {
  projects: readonly SearchProject[];
  branches: readonly SearchBranch[];
  selectedProjectId: string;
  selectedBranchName: string;
  loading: boolean;
  pending: boolean;
  onProjectChange: (projectId: string) => void;
  onBranchChange: (branch: string) => void;
};

export function SearchHeader({
  projects,
  branches,
  selectedProjectId,
  selectedBranchName,
  loading,
  pending,
  onProjectChange,
  onBranchChange,
}: Props) {
  const selectedProject = projects.find(
    (project) => project.id === selectedProjectId,
  );
  const selectedBranch = branches.find(
    (branch) => branch.name === selectedBranchName,
  );

  return (
    <header className="flex min-h-16 shrink-0 flex-wrap items-center gap-4 border-border border-b bg-surface px-4 py-2">
      <div className="flex min-w-48 items-center gap-3">
        <div className="grid size-8 place-items-center border border-border-strong bg-inset text-accent">
          <FileSearch2 className="size-4" />
        </div>
        <div className="min-w-0">
          <p className="font-medium text-badge text-fg-subtle uppercase tracking-[0.14em]">
            Repository intelligence
          </p>
          <h1 className="truncate font-semibold text-title">Investigation</h1>
        </div>
      </div>
      {selectedProject ? (
        <div className="hidden min-w-0 items-center gap-2 border-border border-l pl-4 lg:flex">
          <GitBranch className="size-3.5 shrink-0 text-fg-subtle" />
          <div className="min-w-0">
            <p className="truncate font-medium text-ui">
              {selectedProject.name}
              {selectedBranch ? (
                <span className="font-normal text-fg-muted">
                  {" "}
                  / {selectedBranch.name}
                </span>
              ) : null}
            </p>
            <code className="block max-w-96 truncate text-code text-fg-subtle">
              {selectedProject.repo_path}
            </code>
          </div>
        </div>
      ) : null}
      <div className="ml-auto grid w-full grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] gap-2 sm:flex sm:w-auto sm:items-center sm:gap-3">
        <div className="hidden items-center gap-1.5 text-badge text-fg-subtle sm:flex">
          <LockKeyhole className="size-3" />
          Read-only
        </div>
        <label className="flex min-w-0 flex-col gap-0.5 text-fg-muted text-meta">
          <span className="font-medium text-badge uppercase tracking-[0.12em]">
            Project
          </span>
          <select
            aria-label="Project"
            className="h-[30px] w-full border border-border-strong bg-inset px-2 text-fg text-ui outline-none focus:border-focus disabled:opacity-60 sm:w-44"
            disabled={loading || pending}
            onChange={(event) => onProjectChange(event.target.value)}
            value={selectedProjectId}
          >
            {!projects.length ? <option value="">No projects</option> : null}
            {projects.map((project) => (
              <option key={project.id} value={project.id}>
                {project.name}
              </option>
            ))}
          </select>
        </label>
        <label className="flex min-w-0 flex-col gap-0.5 text-fg-muted text-meta">
          <span className="font-medium text-badge uppercase tracking-[0.12em]">
            Branch
          </span>
          <select
            aria-label="Branch"
            className="h-[30px] w-full border border-border-strong bg-inset px-2 text-fg text-ui outline-none focus:border-focus disabled:opacity-60 sm:w-64"
            disabled={loading || pending || !selectedProject}
            onChange={(event) => onBranchChange(event.target.value)}
            value={selectedBranchName}
          >
            {!branches.length ? (
              <option value="">No local branches</option>
            ) : null}
            {branches.map((branch) => (
              <option
                key={`${branch.name}:${branch.commit}`}
                value={branch.name}
              >
                {branch.name}
                {branch.is_head ? " — HEAD" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>
    </header>
  );
}
