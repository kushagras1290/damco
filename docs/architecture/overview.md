# Architecture overview

JobPulse is a **modular monolith + worker**: one FastAPI service and one Temporal worker share a
framework-free domain package and a PostgreSQL database. Temporal owns durable orchestration;
Redis stores only ephemeral shared state (rate-limit windows, idempotency records, quotas and a
short response cache). Raw fetch snapshots can use local disk or Cloudflare R2. PostgreSQL remains
the system of record.

## Packages

| Package | Depends on | Responsibility |
|---|---|---|
| `jobpulse_core` (`packages/python`) | pydantic, httpx, selectolax, feedparser, nh3, openai | Pure domain: models, adapters, normalizer, eligibility, ranking, intelligence client, notifications, polling policy, Temporal payload contracts and workflow names |
| `jobpulse` (`apps/api`) | core | Config, DB models/repositories, application services (ingestion, evaluation, storage, Temporal client), REST API |
| `jobpulse_worker` (`apps/worker`) | core, api | Workflows (deterministic, sandboxed) and activities (thin adapters over services) |
| `apps/web` | — | Next.js dashboard; talks to the API only through a server-side proxy |

The worker reuses the API package's services and repositories so business logic exists once.
Workflows reference activities **by name**, so the Temporal sandbox never imports I/O code.

## Workflows

```
SourcePollingWorkflow (id = source-polling-{source_id}, one per source, long-lived)
  loop: GetSourceSchedule → (circuit open? sleep) → child SourceDiscoveryWorkflow
        → next_poll() policy → RecordPoll → durable sleep (wakes on `poll_now` signal)
  continue-as-new every 50 iterations

SourceDiscoveryWorkflow
  FetchJobsActivity      fetch via SafeHttpClient, write staging snapshot, save checkpoint (ETag)
  NormalizeJobsActivity  pure normalization, staged to object storage
  StoreJobsActivity      upsert + dedup + versions + per-job raw snapshots, close vanished jobs
  ListEvaluationTargets  subscribed (workspace, profile) pairs
  → start JobEvaluationWorkflow per new/changed job and profile (ABANDON)

JobEvaluationWorkflow
  Eligibility → Enrichment* → Embedding* → Ranking → Notification
  (* degradable: an LLM outage after retries does not block deterministic ranking)
```

Evaluation workflow IDs contain job, profile and content hash, so duplicate starts for the same
decision are idempotent. Payloads carry IDs and storage keys only, keeping workflow histories
small.

## Data model

The catalogue is shared: `companies, sources, source_checkpoints, jobs, job_versions,
raw_snapshots, job_intelligence`. Tenant state is workspace-scoped:
`memberships, profiles, source_subscriptions, profile_jobs, eligibility_decisions,
match_scores, notifications, applications, workflow_runs, audit_events`. Identity and lifecycle
tables include `users, identities, invitations, email_login_tokens, billing_events`.

Decision tables are **append-only**; the latest row wins. `profile_jobs` carries denormalised
per-profile eligibility, score and workflow state for fast filtering. PostgreSQL row-level
security is enabled and forced on tenant tables; a transaction with no workspace or explicit
system scope sees no tenant data.

### Indexes

| Need | Index |
|---|---|
| Filters (company, source, published_at, location, remote_policy, seniority, eligibility, workflow state) | B-tree |
| Keyword search ("Python", "RAG", "Agentic AI") | generated `tsvector` (title weight A, body B) + GIN, `websearch_to_tsquery` |
| Near-duplicate titles ("Sr AI Engineer") | `pg_trgm` GIN on `normalized_title` |
| Semantic similarity | pgvector HNSW (`vector_cosine_ops`) |

## Deduplication

1. `UNIQUE(source_id, external_id)` — same posting re-seen from the same source.
2. Canonical URL — scheme/host/port normalised, tracking parameters stripped, query sorted.
3. Fingerprint `SHA256(registrable_domain | normalized_title | normalized_location)`, with
   trigram similarity ≥ 0.85 inside the same company as a secondary signal.

Cross-source duplicates are stored (for provenance) with `duplicate_of_id` and are never
re-evaluated or re-notified.

## Request path (web)

Browser → Next.js route `/api/backend/[...path]` → validates the path against an allowlist →
reads the Auth.js session and selected-workspace cookie → mints a 5-minute Ed25519 JWT
(`sub`, optional `wid`, `aud`, `iss`; deliberately no role) → FastAPI verifies the signature,
resolves the identity and membership, verifies that the caller belongs to `wid`, and applies that
workspace's RLS scope. Anonymous visitors receive a pseudonymous signed subject and can only read
the public demo workspace. The browser never sees a reusable backend token, and the API holds
public verification keys only.
