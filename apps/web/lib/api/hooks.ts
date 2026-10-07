"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, type QueryParams } from "@/lib/api/client";
import { fallbackRefetchInterval } from "@/lib/stores/live-feed";
import type {
  ActionAccepted,
  Application,
  ApplicationStatus,
  DashboardStats,
  Decision,
  JobDetail,
  JobSummary,
  Me,
  Page,
  Profile,
  Run,
  SnapshotContent,
  Source,
  SystemStatus,
} from "@/lib/api/types";
import type { ProfileFormValues, SourceFormValues } from "@/lib/schemas";

/** Only used while the realtime stream is down; live events invalidate queries otherwise. */
const refetchWhenOffline = fallbackRefetchInterval(15_000);

export const keys = {
  me: ["me"] as const,
  dashboard: ["dashboard"] as const,
  jobs: (params: QueryParams) => ["jobs", params] as const,
  job: (id: string) => ["job", id] as const,
  snapshot: (id: string) => ["job", id, "snapshot"] as const,
  sources: ["sources"] as const,
  source: (id: string) => ["source", id] as const,
  runs: (params: QueryParams) => ["runs", params] as const,
  decisions: (params: QueryParams) => ["decisions", params] as const,
  applications: (params: QueryParams) => ["applications", params] as const,
  profile: ["profile"] as const,
  system: ["system"] as const,
};

export const useMe = () => useQuery({ queryKey: keys.me, queryFn: () => api.get<Me>("/me"), staleTime: 60_000 });

export function useIsOwner(): boolean {
  return useMe().data?.role === "OWNER";
}

export const useDashboard = () =>
  useQuery({ queryKey: keys.dashboard, queryFn: () => api.get<DashboardStats>("/dashboard"), refetchInterval: refetchWhenOffline });

export const useJobs = (params: QueryParams) =>
  useQuery({
    queryKey: keys.jobs(params),
    queryFn: () => api.get<Page<JobSummary>>("/jobs", params),
    placeholderData: keepPreviousData,
  });

export const useJob = (id: string) => useQuery({ queryKey: keys.job(id), queryFn: () => api.get<JobDetail>(`/jobs/${id}`) });

export const useSnapshot = (id: string, enabled: boolean) =>
  useQuery({ queryKey: keys.snapshot(id), queryFn: () => api.get<SnapshotContent>(`/jobs/${id}/snapshot`), enabled });

export const useSources = () =>
  useQuery({
    queryKey: keys.sources,
    queryFn: () => api.get<Page<Source>>("/sources", { limit: 100 }),
    refetchInterval: refetchWhenOffline,
  });

export const useSource = (id: string) =>
  useQuery({ queryKey: keys.source(id), queryFn: () => api.get<Source>(`/sources/${id}`), refetchInterval: refetchWhenOffline });

export const useRuns = (params: QueryParams) =>
  useQuery({
    queryKey: keys.runs(params),
    queryFn: () => api.get<Page<Run>>("/runs", params),
    placeholderData: keepPreviousData,
    refetchInterval: refetchWhenOffline,
  });

export const useDecisions = (params: QueryParams) =>
  useQuery({
    queryKey: keys.decisions(params),
    queryFn: () => api.get<Page<Decision>>("/decisions", params),
    placeholderData: keepPreviousData,
  });

export const useApplications = (params: QueryParams) =>
  useQuery({ queryKey: keys.applications(params), queryFn: () => api.get<Page<Application>>("/applications", params) });

export const useProfile = () => useQuery({ queryKey: keys.profile, queryFn: () => api.get<Profile>("/profile") });

export const useSystem = () =>
  useQuery({ queryKey: keys.system, queryFn: () => api.get<SystemStatus>("/system"), refetchInterval: refetchWhenOffline });

// ------------------------------------------------------------------ mutations

export function useCreateSource() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (values: SourceFormValues) => api.post<Source>("/sources", values),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.sources }),
  });
}

export function useUpdateSource(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (patch: Partial<Pick<Source, "enabled" | "name" | "poll_interval_seconds">>) =>
      api.patch<Source>(`/sources/${id}`, patch),
    onSuccess: (source) => {
      client.setQueryData(keys.source(id), source);
      void client.invalidateQueries({ queryKey: keys.sources });
    },
  });
}

export const useSyncSource = (id: string) =>
  useMutation({ mutationFn: () => api.post<ActionAccepted>(`/sources/${id}/sync`) });

export function useRerunJob(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<ActionAccepted>(`/jobs/${id}/evaluate`),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.job(id) }),
  });
}

export function useTrackApplication(jobId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (status: ApplicationStatus) => api.post<Application>(`/jobs/${jobId}/applications`, { status }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.job(jobId) });
      void client.invalidateQueries({ queryKey: ["applications"] });
    },
  });
}

export function useUpdateApplication() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, status }: { id: string; status: ApplicationStatus }) =>
      api.patch<Application>(`/applications/${id}`, { status }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["applications"] }),
  });
}

export function useUpdateProfile() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (values: ProfileFormValues) => api.patch<Profile>("/profile", values),
    onSuccess: (profile) => client.setQueryData(keys.profile, profile),
  });
}
