import { Search } from "lucide-react";
import { EmptyState } from "@/components/layout/EmptyState";

export function SearchRoute() {
  return (
    <EmptyState
      icon={Search}
      title="Code search is not available yet"
      description="Agentic search over the indexed repository, with every claim citing a clickable path and line."
    />
  );
}
