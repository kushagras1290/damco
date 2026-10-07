import { create } from "zustand";
import { persist } from "zustand/middleware";

import type { EligibilityStatus, RemotePolicy } from "@/lib/api/types";

export const PAGE_SIZE = 25;

export type JobSort = "score" | "published" | "discovered";

interface JobFiltersState {
  query: string;
  eligibility: EligibilityStatus | "";
  remotePolicy: RemotePolicy | "";
  minScore: number;
  sort: JobSort;
  page: number;
  set: (patch: Partial<Omit<JobFiltersState, "set" | "reset">>) => void;
  reset: () => void;
}

const DEFAULTS = {
  query: "",
  eligibility: "eligible" as const,
  remotePolicy: "" as const,
  minScore: 0,
  sort: "score" as const,
  page: 0,
};

export const useJobFilters = create<JobFiltersState>()(
  persist(
    (set) => ({
      ...DEFAULTS,
      // Any filter change other than paging returns to the first page.
      set: (patch) => set((state) => ({ ...state, ...patch, page: patch.page ?? 0 })),
      reset: () => set(DEFAULTS),
    }),
    {
      name: "jobpulse-job-filters",
      partialize: ({ query, eligibility, remotePolicy, minScore, sort }) => ({
        query,
        eligibility,
        remotePolicy,
        minScore,
        sort,
      }),
    },
  ),
);
