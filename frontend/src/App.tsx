import { QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { RouterProvider } from "react-router";
import { createQueryClient } from "@/api/query-client";
import { TooltipProvider } from "@/components/ui/tooltip";
import { router } from "@/routes/router";
import { WebSocketProvider } from "@/ws/WebSocketProvider";

/**
 * The three state sources of docs/architecture.md §6, wired once and never
 * mixed: TanStack Query for server state, one WebSocket connection feeding
 * Zustand for live state, component state for UI state.
 */
export function App() {
  const [queryClient] = useState(createQueryClient);

  return (
    <QueryClientProvider client={queryClient}>
      <WebSocketProvider>
        <TooltipProvider>
          <RouterProvider router={router} />
        </TooltipProvider>
      </WebSocketProvider>
    </QueryClientProvider>
  );
}
