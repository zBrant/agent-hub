import { useCallback } from "react";
import { Outlet } from "react-router";
import { TopBar } from "@/components/layout/TopBar";
import { useConnectionStore } from "@/stores/connection-store";
import { useWebSocketClient } from "@/ws/WebSocketProvider";

/**
 * Persistent application frame. The navigation becomes a compact operations
 * rail on desktop and remains a top bar on narrow screens.
 *
 * Reads live state with narrow selectors and hands plain props down: components
 * under `components/` know nothing about stores or the API
 * (docs/architecture.md §6).
 */
export function RootLayout() {
  const status = useConnectionStore((state) => state.status);
  const attempt = useConnectionStore((state) => state.attempt);
  const client = useWebSocketClient();

  const handleReconnect = useCallback(() => {
    client?.reconnectNow();
  }, [client]);

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-bg text-fg md:flex-row">
      <TopBar status={status} attempt={attempt} onReconnect={handleReconnect} />
      <main className="min-h-0 min-w-0 flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}
