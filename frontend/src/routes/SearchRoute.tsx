import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { type AgentCitation, type AgentEvidence, api } from "@/api/client";
import type { CitationTarget } from "@/components/search/SearchCodePanel";
import type { SearchTurn } from "@/components/search/SearchExchange";
import { SearchHeader } from "@/components/search/SearchHeader";
import { SearchWorkspace } from "@/components/search/SearchWorkspace";
import {
  resolveSearchBranch,
  resolveSearchProject,
} from "@/lib/search-projects";

export function SearchRoute() {
  const discovered = useQuery({
    queryKey: ["search-projects"],
    queryFn: api.listSearchProjects,
  });
  const [projectId, setProjectId] = useState("");
  const [branchName, setBranchName] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<SearchTurn[]>([]);
  const [target, setTarget] = useState<CitationTarget | null>(null);
  const nextTurn = useRef(0);
  const projects = discovered.data?.projects ?? [];
  const selectedProject = resolveSearchProject(projects, projectId);
  const selectedBranch = resolveSearchBranch(selectedProject, branchName);
  const selectedProjectId = selectedProject?.id ?? "";
  const selectedBranchName = selectedBranch?.name ?? "";

  const answer = useMutation({
    mutationFn: (request: {
      id: number;
      projectId: string;
      branch: string;
      question: string;
    }) =>
      api.answerSearchQuestion(
        request.projectId,
        request.branch,
        request.question,
      ),
    onSuccess: (result, request) => {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === request.id ? { ...turn, answer: result } : turn,
        ),
      );
    },
    onError: (error, request) => {
      setTurns((current) =>
        current.map((turn) =>
          turn.id === request.id ? { ...turn, error: error.message } : turn,
        ),
      );
    },
  });
  const source = useMutation({
    mutationFn: (citation: CitationTarget) =>
      api.readSearchFile(selectedProjectId, selectedBranchName, citation.path, {
        startLine: citation.line,
        endLine: citation.endLine,
      }),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const clean = question.trim();
    if (!clean || !selectedProjectId || !selectedBranchName || answer.isPending)
      return;
    const id = ++nextTurn.current;
    setTurns((current) => [...current, { id, question: clean }]);
    setQuestion("");
    answer.mutate({
      id,
      projectId: selectedProjectId,
      branch: selectedBranchName,
      question: clean,
    });
  }

  function resetInvestigation() {
    setTurns([]);
    closeSource();
  }

  function changeProject(value: string) {
    setProjectId(value);
    setBranchName("");
    resetInvestigation();
  }

  function changeBranch(value: string) {
    setBranchName(value);
    resetInvestigation();
  }

  function openCitation(citation: AgentCitation) {
    openSource({
      path: citation.path,
      line: citation.line,
      endLine: citation.end_line,
      expectedHash: citation.content_hash,
    });
  }

  function openEvidence(evidence: AgentEvidence) {
    openSource({
      path: evidence.path,
      line: evidence.line,
      endLine: evidence.end_line,
      expectedHash: null,
    });
  }

  function openSource(citation: CitationTarget) {
    setTarget(citation);
    source.mutate(citation);
  }

  function closeSource() {
    setTarget(null);
    source.reset();
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <SearchHeader
        loading={discovered.isLoading}
        branches={selectedProject?.branches ?? []}
        onBranchChange={changeBranch}
        onProjectChange={changeProject}
        pending={answer.isPending}
        projects={projects}
        selectedBranchName={selectedBranchName}
        selectedProjectId={selectedProjectId}
      />
      {discovered.error ? (
        <p className="p-4 text-failed text-meta" role="alert">
          {discovered.error.message}
        </p>
      ) : (
        <SearchWorkspace
          disabled={!selectedProjectId || !selectedBranchName}
          file={source.data}
          onCloseSource={closeSource}
          onOpenCitation={openCitation}
          onOpenEvidence={openEvidence}
          onQuestionChange={setQuestion}
          onSubmit={submit}
          pending={answer.isPending}
          question={question}
          sourceError={source.error}
          sourceLoading={source.isPending}
          target={target}
          turns={turns}
        />
      )}
    </div>
  );
}
