import { create } from "zustand";
import type { SystemMetricsPayload } from "@/ws/protocol";

const SAMPLE_CAPACITY = 300;

type SystemMetricsState = {
  samples: readonly SystemMetricsPayload[];
  latest: SystemMetricsPayload | null;
  push: (snapshot: SystemMetricsPayload) => void;
  reset: () => void;
};

/** Bounded live state for the singleton `metrics` WebSocket topic. */
export const useSystemMetricsStore = create<SystemMetricsState>((set) => ({
  samples: [],
  latest: null,
  push: (snapshot) =>
    set((state) => {
      if (state.latest?.ts === snapshot.ts) {
        return {
          samples: [...state.samples.slice(0, -1), snapshot],
          latest: snapshot,
        };
      }
      return {
        samples: [...state.samples.slice(-(SAMPLE_CAPACITY - 1)), snapshot],
        latest: snapshot,
      };
    }),
  reset: () => set({ samples: [], latest: null }),
}));
