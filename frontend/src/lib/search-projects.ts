import type { SearchBranch, SearchProject } from "@/api/client";

/** Keep a valid selection while project discovery refreshes underneath the UI. */
export function resolveSearchProject(
  projects: readonly SearchProject[],
  requestedId: string,
): SearchProject | undefined {
  return projects.find((project) => project.id === requestedId) ?? projects[0];
}

/** Prefer an explicit branch, then the repository HEAD, then the first branch. */
export function resolveSearchBranch(
  project: SearchProject | undefined,
  requestedName: string,
): SearchBranch | undefined {
  if (!project) return undefined;
  return (
    project.branches.find((branch) => branch.name === requestedName) ??
    project.branches.find((branch) => branch.is_head) ??
    project.branches[0]
  );
}
