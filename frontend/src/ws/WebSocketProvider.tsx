import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useState,
} from "react";
import { useConnectionStore } from "@/stores/connection-store";
import { defaultWebSocketUrl, WebSocketClient } from "@/ws/client";

const WebSocketContext = createContext<WebSocketClient | null>(null);

type Props = {
  children: ReactNode;
  /** Override for tests; defaults to same-origin `/ws`. */
  url?: string;
};

/**
 * Owns the application's one and only WebSocket connection.
 *
 * Mounted once at the root. `useEffect` here is legitimate: it synchronizes
 * with an external system and returns a cleanup, which is the only reason
 * docs/conventions.md §3 allows one.
 */
export function WebSocketProvider({ children, url }: Props) {
  const [client, setClient] = useState<WebSocketClient | null>(null);

  useEffect(() => {
    // A disposed client is not reusable, so StrictMode's double-invoke — and a
    // changed `url` — must produce a new instance, not revive the old one.
    const instance = new WebSocketClient({
      url: url ?? defaultWebSocketUrl(),
      onStatus: useConnectionStore.getState().setStatus,
    });
    setClient(instance);
    instance.connect();
    return () => {
      instance.dispose();
      setClient(null);
    };
  }, [url]);

  return (
    <WebSocketContext.Provider value={client}>
      {children}
    </WebSocketContext.Provider>
  );
}

/**
 * The shared client, or `null` before the provider's effect has run.
 * Callers subscribe inside a `useEffect` that guards on `null`.
 */
export function useWebSocketClient(): WebSocketClient | null {
  return useContext(WebSocketContext);
}
