import { QueryClient } from "@tanstack/react-query";

/**
 * Server state only (docs/architecture.md §6). Anything that arrives over the
 * WebSocket is never polled: structural events invalidate a query, stream
 * events go to a Zustand store.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // The backend is a local process pushing changes over `/ws`; polling or
        // refetching on focus would duplicate work the broker already does.
        refetchOnWindowFocus: false,
        refetchOnReconnect: false,
        staleTime: 30_000,
        retry: 1,
      },
    },
  });
}
