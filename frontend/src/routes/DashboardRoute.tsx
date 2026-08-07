import { LayoutDashboard } from "lucide-react";
import { EmptyState } from "@/components/layout/EmptyState";

export function DashboardRoute() {
  return (
    <EmptyState
      icon={LayoutDashboard}
      title="No active sessions"
      description="Token totals, estimated equivalent cost and system health appear here once a session is running."
    />
  );
}
