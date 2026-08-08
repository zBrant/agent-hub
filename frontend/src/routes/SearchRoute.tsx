import { useMutation, useQuery } from "@tanstack/react-query";
import { type FormEvent, useRef, useState } from "react";
import { type AgentCitation, type AgentEvidence, api } from "@/api/client";
import type { CitationTarget } from "@/components/search/SearchCodePanel";
import type { SearchTurn } from "@/components/search/SearchConversation";
import { SearchHeader } from "@/components/search/SearchHeader";
import { SearchWorkspace } from "@/components/search/SearchWorkspace";

export function SearchRoute() {
  const sessions = useQuery({
    queryKey: ["sessions"],
    queryFn: api.listSessions,
  });
  const [sessionId, setSessionId] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<SearchTurn[]>([]);
  const [target, setTarget] = useState<CitationTarget | null>(null);
  const nextTurn = useRef(0);
  const selectedId = sessionId || sessions.data?.[0]?.id || "";

  const answer = useMutation({
    mutationFn: (request: {
      id: number;
      sessionId: string;
      question: string;
    }) => api.answerSearchQuestion(request.sessionId, request.question),
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
      api.readSearchFile(selectedId, citation.path, {
        startLine: citation.line,
        endLine: citation.endLine,
      }),
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const clean = question.trim();
    if (!clean || !selectedId || answer.isPending) return;
    const id = ++nextTurn.current;
    setTurns((current) => [...current, { id, question: clean }]);
    setQuestion("");
    answer.mutate({ id, sessionId: selectedId, question: clean });
  }

  function changeSession(value: string) {
    setSessionId(value);
    setTurns([]);
    closeSource();
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
        loading={sessions.isLoading}
        onChange={changeSession}
        pending={answer.isPending}
        selectedId={selectedId}
        sessions={sessions.data ?? []}
      />
      {sessions.error ? (
        <p className="p-4 text-failed text-meta" role="alert">
          {sessions.error.message}
        </p>
      ) : (
        <SearchWorkspace
          disabled={!selectedId}
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
