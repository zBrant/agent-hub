import { create } from "zustand";
import type { ConnectionStatus } from "@/ws/client";

/**
 * Live state for the single WebSocket connection itself.
 *
 * Not a topic store — topic stores (one per topic, docs/conventions.md §1)
 * arrive with B9, when there are messages to put in them. This one exists
 * because the shell has to tell the user the backend is unreachable.
 *
 * Read it with a narrow selector: `useConnectionStore(s => s.status)`.
 */
type ConnectionState = {
  status: ConnectionStatus;
  /** Consecutive failed connection attempts; 0 while connected. */
  attempt: number;
  setStatus: (status: ConnectionStatus, attempt: number) => void;
};

export const useConnectionStore = create<ConnectionState>((set) => ({
  status: "idle",
  attempt: 0,
  setStatus: (status, attempt) => set({ status, attempt }),
}));
