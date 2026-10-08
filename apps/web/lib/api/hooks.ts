"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { ApiError, api, type QueryParams } from "@/lib/api/client";
import { fallbackRefetchInterval } from "@/lib/stores/live-feed";
import type {
  ActionAccepted,
  Application,
  ApplicationStatus,
  Billing,
  DashboardStats,
  Decision,
  JobDetail,
  JobSummary,
  Invitation,
  InvitationIssued,
  InvitableRole,
  Me,
  Member,
  Page,
  Problem,
  Profile,
  Run,
  SnapshotContent,
  Source,
  SystemStatus,
  WorkspaceInfo,
  WorkspaceRole,
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
  members: ["workspace", "members"] as const,
  invitations: ["workspace", "invitations"] as const,
};

export const useMe = () => useQuery({ queryKey: keys.me, queryFn: () => api.get<Me>("/me"), staleTime: 60_000 });

const ROLE_RANK: Record<WorkspaceRole, number> = { viewer: 0, member: 1, admin: 2, owner: 3 };

/** UI gating only - the API enforces the same rule from the database on every request. */
export function useCan(minimum: Exclude<WorkspaceRole, "viewer">): boolean {
  const role = useMe().data?.role;
  return role != null && ROLE_RANK[role] >= ROLE_RANK[minimum];
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

export function useUnfollowSource(id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.delete(`/sources/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.sources }),
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

// ------------------------------------------------------------------ workspaces

export const useMembers = (enabled: boolean) =>
  useQuery({ queryKey: keys.members, queryFn: () => api.get<Member[]>("/workspace/members"), enabled });

export const useInvitations = (enabled: boolean) =>
  useQuery({ queryKey: keys.invitations, queryFn: () => api.get<Invitation[]>("/workspace/invitations"), enabled });

export function useInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { role: InvitableRole; email?: string }) => api.post<InvitationIssued>("/workspace/invitations", body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.invitations }),
  });
}

export function useRevokeInvitation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.delete(`/workspace/invitations/${id}`),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.invitations }),
  });
}

export function useChangeMemberRole() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: InvitableRole }) =>
      api.patch<Member>(`/workspace/members/${userId}`, { role }),
    onSuccess: () => client.invalidateQueries({ queryKey: ["workspace"] }),
  });
}

export function useRemoveMember() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (userId: string) => api.delete(`/workspace/members/${userId}`),
    onSuccess: () => client.invalidateQueries(),
  });
}

export function useRenameWorkspace() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => api.patch<WorkspaceInfo>("/workspace", { name }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.me }),
  });
}

export const useCreateWorkspace = () =>
  useMutation({ mutationFn: (name: string) => api.post<WorkspaceInfo>("/workspaces", { name }) });

export const useAcceptInvitation = () =>
  useMutation({ mutationFn: (token: string) => api.post<{ workspace_id: string }>("/invitations/accept", { token }) });

// ------------------------------------------------------------------ billing

export const useBilling = (enabled: boolean) =>
  useQuery({ queryKey: ["workspace", "billing"], queryFn: () => api.get<Billing>("/billing"), enabled });

/** Starts a Razorpay subscription and hands the browser to Razorpay's hosted checkout. */
export const useCheckout = () =>
  useMutation({
    mutationFn: (plan: "pro" | "team") => api.post<{ checkout_url: string }>("/billing/checkout", { plan }),
    onSuccess: ({ checkout_url }) => {
      if (checkout_url.startsWith("https://")) window.location.assign(checkout_url);
    },
  });

export function useCancelSubscription() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<{ status: string }>("/billing/cancel"),
    onSuccess: () => client.invalidateQueries({ queryKey: ["workspace", "billing"] }),
  });
}

export const useDeleteWorkspace = () =>
  useMutation({
    mutationFn: async (confirmName: string) => {
      const response = await fetch("/api/backend/workspace", {
        method: "DELETE",
        headers: { "content-type": "application/json", "idempotency-key": crypto.randomUUID() },
        body: JSON.stringify({ confirm_name: confirmName }),
        credentials: "same-origin",
      });
      if (!response.ok) {
        const problem = (await response.json().catch(() => null)) as Problem | null;
        throw new ApiError(response.status, problem);
      }
    },
  });
