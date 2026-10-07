# ADR 0008 — Multi-tenancy: shared catalogue, workspace-scoped data, PostgreSQL RLS

**Status:** Accepted · design: [multi-tenancy.md](../design/multi-tenancy.md)

## Context
JobPulse becomes a product sold to individual job seekers and to recruiting teams. Customers
must never see each other's profiles, verdicts, scores, alerts or applications, while job
postings themselves are public and expensive to fetch and enrich.

## Decision
- **Workspaces** are the tenant boundary (a job seeker is a one-person workspace).
- **Shared catalogue**: companies, sources, jobs, versions, snapshots, AI intelligence and job
  embeddings are global and computed once. Workspaces follow sources via
  `source_subscriptions`.
- **Per-profile state** moves off the shared `jobs` row into `profile_jobs`
  (eligibility, score, evaluation state). Discovery fans out one evaluation per
  (job, subscribed profile).
- **Isolation by PostgreSQL row-level security**, not by convention: every tenant table
  carries `workspace_id`; RLS is enabled and forced; each transaction switches to the
  non-owner role `jobpulse_app` and sets its scope; the default scope sees nothing.

## Alternatives rejected
- Application-level filtering only: one forgotten `WHERE` leaks data; RLS makes the
  database enforce it and fail closed.
- Schema- or database-per-tenant: stronger separation but per-tenant migrations and a
  duplicated catalogue (N× fetch and LLM cost).

## Consequences
- One extra statement per transaction (scope + role in a single `set_config` call).
- Migrations run as the owner; the app role needs `GRANT`s, which migration 0002 sets up,
  including default privileges for future tables.
- Code must choose a scope explicitly (workspace or system). System scope is limited to
  ingestion and discovery fan-out and is never derived from request input.
- Contract follow-up: the unused legacy columns `jobs.eligibility_status` and
  `jobs.match_score` are dropped in the next release (expand/contract).
