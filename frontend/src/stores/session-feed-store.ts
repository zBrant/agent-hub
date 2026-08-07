import { create } from "zustand";
import type { AgentEvent } from "@/api/events";

type SessionFeedState = {
  eventsByRun: Readonly<Record<string, readonly AgentEvent[]>>;
  append: (event: AgentEvent) => void;
  hydrate: (runId: string, persisted: readonly AgentEvent[]) => void;
};

function eventKey(event: AgentEvent): string {
  return JSON.stringify(event);
}

function mergePersistedAndLive(
  persisted: readonly AgentEvent[],
  live: readonly AgentEvent[],
): readonly AgentEvent[] {
  const maximum = Math.min(persisted.length, live.length);
  let overlap = 0;
  for (let size = maximum; size > 0; size -= 1) {
    const persistedStart = persisted.length - size;
    let equal = true;
    for (let index = 0; index < size; index += 1) {
      const left = persisted[persistedStart + index];
      const right = live[index];
      if (!left || !right || eventKey(left) !== eventKey(right)) {
        equal = false;
        break;
      }
    }
    if (equal) {
      overlap = size;
      break;
    }
  }
  return [...persisted, ...live.slice(overlap)];
}

export const useSessionFeedStore = create<SessionFeedState>((set) => ({
  eventsByRun: {},
  append: (event) =>
    set((state) => ({
      eventsByRun: {
        ...state.eventsByRun,
        [event.run_id]: [...(state.eventsByRun[event.run_id] ?? []), event],
      },
    })),
  hydrate: (runId, persisted) =>
    set((state) => ({
      eventsByRun: {
        ...state.eventsByRun,
        [runId]: mergePersistedAndLive(
          persisted,
          state.eventsByRun[runId] ?? [],
        ),
      },
    })),
}));
