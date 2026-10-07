// Mirrors apps/api/src/jobpulse/api/schemas.py

export type EligibilityStatus = "pending" | "eligible" | "ineligible";
export type RemotePolicy = "remote" | "hybrid" | "onsite" | "unknown";
export type RuleOutcome = "pass" | "fail" | "unknown";
export type ApplicationStatus = "interested" | "applied" | "interviewing" | "offer" | "rejected" | "withdrawn";
export type SourceKind = "greenhouse" | "lever" | "ashby" | "rss" | "generic_json" | "static_html" | "dynamic_html";
export type Seniority =
  | "intern"
  | "junior"
  | "mid"
  | "senior"
  | "staff"
  | "principal"
  | "lead"
  | "manager"
  | "director"
  | "unknown";

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface JobSummary {
  id: string;
  title: string;
  company: string;
  company_domain: string;
  source_id: string;
  source_name: string;
  source_kind: string;
  location: string | null;
  remote_policy: RemotePolicy;
  seniority: Seniority;
  url: string;
  published_at: string | null;
  first_seen_at: string;
  eligibility_status: EligibilityStatus;
  workflow_state: string;
  match_score: number | null;
  closed: boolean;
}

export interface Rule {
  rule: string;
  outcome: RuleOutcome;
  evidence: string;
  source: "deterministic" | "ai";
}

export interface EligibilityDecision {
  id: string;
  stage: "deterministic" | "post_enrichment";
  status: EligibilityStatus;
  rules: Rule[];
  unresolved: string[];
  policy_hash: string;
  content_hash: string;
  workflow_id: string | null;
  created_at: string;
}

export interface Intelligence {
  model: string;
  data: Record<string, unknown>;
  confidence: number;
  escalated: boolean;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  created_at: string;
}

export interface ScoreComponent {
  name: string;
  value: number | null;
  weight: number;
  detail: string;
}

export interface Score {
  final_score: number;
  actionable: boolean;
  components: ScoreComponent[];
  weights: Record<string, number>;
  matched_skills: string[];
  missing_skills: string[];
  workflow_id: string | null;
  created_at: string;
}

export interface Snapshot {
  id: string;
  snapshot_key: string;
  snapshot_hash: string;
  content_type: string;
  size_bytes: number;
  fetched_at: string;
}

export interface Application {
  id: string;
  job_id: string;
  job_title: string;
  company: string;
  status: ApplicationStatus;
  notes: string;
  applied_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface JobDetail extends JobSummary {
  department: string | null;
  employment_type: string | null;
  description_html: string;
  description_text: string;
  version: number;
  content_hash: string;
  fingerprint: string;
  duplicate_of_id: string | null;
  eligibility: EligibilityDecision | null;
  eligibility_history: EligibilityDecision[];
  intelligence: Intelligence | null;
  score: Score | null;
  snapshot: Snapshot | null;
  versions: { version: number; content_hash: string; title: string; location: string | null; created_at: string }[];
  notifications: {
    channel: string;
    status: string;
    attempts: number;
    error: string | null;
    sent_at: string | null;
    created_at: string;
  }[];
  application: Application | null;
  similar: { id: string; title: string; company: string; similarity: number }[];
}

export interface SnapshotContent {
  snapshot: Snapshot;
  content: string;
  truncated: boolean;
}

export interface Source {
  id: string;
  name: string;
  kind: SourceKind;
  company: string;
  company_domain: string;
  config: Record<string, unknown>;
  enabled: boolean;
  poll_interval_seconds: number;
  min_poll_interval_seconds: number;
  max_poll_interval_seconds: number;
  consecutive_failures: number;
  circuit_open_until: string | null;
  last_polled_at: string | null;
  last_success_at: string | null;
  last_new_job_at: string | null;
  last_error: string | null;
  open_jobs: number;
  created_at: string;
}

export interface Run {
  id: string;
  workflow_id: string;
  run_id: string | null;
  workflow_type: string;
  source_id: string | null;
  job_id: string | null;
  status: "running" | "completed" | "failed" | "cancelled";
  stats: Record<string, unknown>;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface EligibilityPolicy {
  allowed_locations: string[];
  allowed_work_models: RemotePolicy[];
  allowed_timezones: string[];
  experience: { min: number; max: number };
  excluded_regions: string[];
  experience_tolerance_years: number;
}

export interface Profile {
  id: string;
  display_name: string;
  target_roles: string[];
  skills: string[];
  years_experience: number;
  seniority: Seniority;
  home_country: string;
  timezone: string;
  summary: string;
  policy: EligibilityPolicy;
  notification_email: string | null;
  webhook_url: string | null;
  notify_min_score: number;
  notifications_enabled: boolean;
  has_embedding: boolean;
  updated_at: string;
}

export interface Decision {
  id: string;
  job_id: string;
  job_title: string;
  company: string;
  stage: string;
  status: EligibilityStatus;
  failed_rules: Rule[];
  unresolved: string[];
  created_at: string;
}

export interface DashboardStats {
  jobs_by_status: Record<string, number>;
  sources_total: number;
  discovered_per_day: { day: string; count: number }[];
  score_histogram: { bucket: number; count: number }[];
  rejection_reasons: { rule: string; count: number }[];
  runs_last_24h: Record<string, number>;
  notifications: Record<string, number>;
  llm_cost_usd: number;
  top_matches: JobSummary[];
}

export interface SystemStatus {
  environment: string;
  version: string;
  intelligence_enabled: boolean;
  storage_backend: string;
  models: Record<string, string | null>;
  dependencies: { name: string; ok: boolean; detail: string }[];
}

export interface Me {
  subject: string;
  role: "PUBLIC_DEMO" | "OWNER";
  authenticated: boolean;
}

export interface ActionAccepted {
  workflow_id: string;
  status: "accepted";
}

export interface Problem {
  title: string;
  status: number;
  detail: string;
  errors?: { loc: string[]; msg: string }[];
}
