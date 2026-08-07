import { Workflow } from "lucide-react";
import { useParams } from "react-router";
import { EmptyState } from "@/components/layout/EmptyState";

export function SessionRoute() {
  const { id } = useParams();

  return (
    <EmptyState
      icon={Workflow}
      title="Session view not built yet"
      description={
        <>
          Node status, the structured event feed and the run controls land with
          the live session view.
          {id ? (
            <>
              {" "}
              Requested session <code className="text-code text-fg">{id}</code>.
            </>
          ) : null}
        </>
      }
    />
  );
}
