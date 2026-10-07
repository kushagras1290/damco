import { create } from "zustand";

import type { LiveEvent } from "@/lib/api/events";

export const MAX_FEED_ITEMS = 50;
export const MAX_TOASTS = 3;
/** A sync with no follow-up event within this window is assumed finished (event lost). */
export const SYNC_EXPIRY_MS = 30_000;
const IN_FLIGHT_STAGES = new Set(["fetching", "fetched"]);

/** connecting -> live; offline = reconnecting after a drop; unavailable = server has realtime off. */
export type LiveStatus = "connecting" | "live" | "offline" | "unavailable";

export interface FeedItem {
  id: number;
  event: LiveEvent;
}

export interface SourceProgress {
  stage: string;
  seq: number;
}

interface LiveFeedState {
  status: LiveStatus;
  items: FeedItem[];
  toasts: FeedItem[];
  sourceProgress: Record<string, SourceProgress>;
  setStatus: (status: LiveStatus) => void;
  push: (event: LiveEvent) => void;
  dismissToast: (id: number) => void;
  expireProgress: (sourceId: string, seq: number) => void;
  clear: () => void;
}

let sequence = 0;

export const useLiveFeed = create<LiveFeedState>()((set, get) => ({
  status: "connecting",
  items: [],
  toasts: [],
  sourceProgress: {},
  setStatus: (status) => set({ status }),
  push: (event) =>
    set((state) => {
      const item = { id: ++sequence, event };
      const next: Partial<LiveFeedState> = { items: [item, ...state.items].slice(0, MAX_FEED_ITEMS) };
      if (event.type === "job.matched") next.toasts = [...state.toasts, item].slice(-MAX_TOASTS);
      if (event.type === "source.progress" || event.type === "jobs.discovered") {
        const stage = event.type === "jobs.discovered" ? "stored" : event.data.stage;
        const sourceId = event.data.source_id;
        next.sourceProgress = { ...state.sourceProgress, [sourceId]: { stage, seq: item.id } };
        if (IN_FLIGHT_STAGES.has(stage)) setTimeout(() => get().expireProgress(sourceId, item.id), SYNC_EXPIRY_MS);
      }
      return next;
    }),
  expireProgress: (sourceId, seq) =>
    set((state) => {
      if (state.sourceProgress[sourceId]?.seq !== seq) return {}; // superseded by a newer event
      const remaining = { ...state.sourceProgress };
      delete remaining[sourceId];
      return { sourceProgress: remaining };
    }),
  dismissToast: (id) => set((state) => ({ toasts: state.toasts.filter((toast) => toast.id !== id) })),
  clear: () => set({ items: [], toasts: [], sourceProgress: {} }),
}));

/** Polling is only a fallback: while the stream is live, events drive cache invalidation. */
export function fallbackRefetchInterval(intervalMs: number): () => number | false {
  return () => (useLiveFeed.getState().status === "live" ? false : intervalMs);
}
