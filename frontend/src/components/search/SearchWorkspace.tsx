import { type FormEvent, useEffect, useState } from "react";
import { Group, type Layout, Panel, Separator } from "react-resizable-panels";
import type { AgentCitation, AgentEvidence, FileRead } from "@/api/client";
import {
  type CitationTarget,
  SearchCodePanel,
} from "@/components/search/SearchCodePanel";
import {
  SearchConversation,
  type SearchTurn,
} from "@/components/search/SearchConversation";

type Props = {
  turns: readonly SearchTurn[];
  question: string;
  pending: boolean;
  disabled: boolean;
  target: CitationTarget | null;
  file: FileRead | undefined;
  sourceLoading: boolean;
  sourceError: Error | null;
  onQuestionChange: (question: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onOpenCitation: (citation: AgentCitation) => void;
  onOpenEvidence: (evidence: AgentEvidence) => void;
  onCloseSource: () => void;
};

const DEFAULT_LAYOUT = { conversation: 55, source: 45 };

export function SearchWorkspace(props: Props) {
  const narrow = useNarrowLayout();
  const orientation = narrow ? "vertical" : "horizontal";
  const layoutKey = `agenthub.search.layout.${narrow ? "narrow" : "wide"}`;

  return (
    <Group
      className="min-h-0 flex-1"
      defaultLayout={loadLayout(layoutKey)}
      id={`search-${orientation}`}
      key={orientation}
      onLayoutChanged={(layout, metadata) => {
        if (metadata.isUserInteraction) saveLayout(layoutKey, layout);
      }}
      orientation={orientation}
    >
      <Panel id="conversation" minSize={narrow ? "240px" : "360px"}>
        <SearchConversation
          disabled={props.disabled}
          onOpenCitation={props.onOpenCitation}
          onOpenEvidence={props.onOpenEvidence}
          onQuestionChange={props.onQuestionChange}
          onSubmit={props.onSubmit}
          pending={props.pending}
          question={props.question}
          turns={props.turns}
        />
      </Panel>
      <Separator
        aria-label="Resize search and source panels"
        className={
          narrow
            ? "h-1 bg-border hover:bg-focus focus:bg-focus"
            : "w-1 bg-border hover:bg-focus focus:bg-focus"
        }
      />
      <Panel id="source" minSize={narrow ? "180px" : "320px"}>
        <SearchCodePanel
          error={props.sourceError}
          file={props.sourceLoading ? undefined : props.file}
          loading={props.sourceLoading}
          onClose={props.onCloseSource}
          target={props.target}
        />
      </Panel>
    </Group>
  );
}

function loadLayout(key: string): Layout {
  try {
    const value: unknown = JSON.parse(
      window.localStorage.getItem(key) ?? "null",
    );
    if (
      typeof value === "object" &&
      value !== null &&
      "conversation" in value &&
      typeof value.conversation === "number" &&
      "source" in value &&
      typeof value.source === "number"
    ) {
      return { conversation: value.conversation, source: value.source };
    }
  } catch {
    // Private browsing or corrupt preferences use the product default.
  }
  return DEFAULT_LAYOUT;
}

function saveLayout(key: string, layout: Layout) {
  try {
    window.localStorage.setItem(key, JSON.stringify(layout));
  } catch {
    // Resizing remains functional when storage is unavailable.
  }
}

function useNarrowLayout(): boolean {
  const [narrow, setNarrow] = useState(false);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia("(max-width: 767px)");
    const update = () => setNarrow(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return narrow;
}
