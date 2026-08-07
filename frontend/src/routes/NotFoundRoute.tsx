import { FileQuestion } from "lucide-react";
import { Link } from "react-router";
import { EmptyState } from "@/components/layout/EmptyState";

export function NotFoundRoute() {
  return (
    <EmptyState
      icon={FileQuestion}
      title="No such page"
      description={
        <Link className="text-accent hover:text-accent-hover" to="/dashboard">
          Back to the dashboard
        </Link>
      }
    />
  );
}
