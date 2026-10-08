# Design: multi-tenant JobPulse (Phase 2)

**Status:** Accepted (2026-10-07). Decisions:

| Question | Decision |
|---|---|
| Customer | Both, via workspaces (a job seeker is a 1-person workspace; teams share one) |
| Isolation | Shared database, `workspace_id` + PostgreSQL row-level security |
| Sign-in | GitHub, Google, email magic link, Microsoft Entra ID |
| Billing | Plan limits enforced now; payments via a provider interface: **Razorpay** first (INR, UPI, cards, India-first), **Stripe** second (international) |

## Goal
Many independent customers on one deployment, each seeing only their own profile,
decisions, matches, applications and alerts, with per-plan limits, without multiplying
fetch and LLM cost per customer.

## Core idea: shared catalogue, tenant-scoped judgement

| Layer | Scope | Tables | Why |
|---|---|---|---|
| **Catalogue** | global | `companies`, `sources`, `source_checkpoints`, `jobs`, `job_versions`, `raw_snapshots`, `job_intelligence` | Job postings are public. Fetch each board once and enrich each posting once (LLM facts are about the job, not the viewer), however many customers follow it. |
| **Tenant** | per workspace | `workspaces`, `memberships`, `invitations`, `profiles`, `source_subscriptions`, `eligibility_decisions`, `match_scores`, `notifications`, `applications`, `audit_events`, `usage_counters` | Everything that reflects a customer's preferences or actions. |

Most tenant tables already key on `profile_id`; we add `workspace_id` (denormalised for
row-level security and indexing) and new tables for workspaces and membership.

```
workspace ─┬─ memberships (user, role: owner | admin | member)
           ├─ profiles (1..n: one per candidate being matched)
           ├─ source_subscriptions ──► sources (global, deduplicated by kind+locator)
           └─ decisions / scores / notifications / applications (per profile)
```

## Isolation
Recommended: **shared database, `workspace_id` on every tenant row, enforced by PostgreSQL
row-level security.**

- Every transaction starts with one `set_config` round trip that switches to the non-owner
  role `jobpulse_app` and sets `app.workspace_id` / `app.system_scope` transaction-locally
  (works with Neon's transaction pooler). Superusers and table owners bypass RLS, hence the
  role switch; RLS is also `FORCE`d on every tenant table.
- Policy on each tenant table: `system_scope = 'on' OR workspace_id = app.workspace_id`
  for both reads (`USING`) and writes (`WITH CHECK`). `workspace_id` defaults to the
  transaction's workspace, so inserts are stamped automatically.
- **Fail closed**: with no scope, tenant tables return nothing and reject writes. The API
  scopes each request to the caller's workspace; evaluation runs in the workspace carried by
  its `JobRef`; only ingestion and discovery fan-out (never request-driven) use system scope.
- Within a workspace, per-job lookups additionally filter by `profile_id` (a workspace can
  hold several candidate profiles).
- Verified by `tests/integration/test_tenancy.py`: app role cannot bypass RLS, no-scope reads
  are empty and writes rejected, cross-workspace writes rejected, forged profile references
  fail, the same job gets different verdicts per workspace, and the API exposes only its own
  workspace.
- Alternatives: schema-per-tenant (painful migrations at 100s of tenants) or
  database-per-tenant (strongest isolation, highest cost; only for enterprise contracts).

## Pipeline changes
1. **Sources are deduplicated** by `(kind, normalised locator)`. Adding a board another
   workspace already follows creates a subscription, not a second poller. Polling stops when the
   last subscription is removed.
2. **Evaluation fans out**: `JobEvaluationWorkflow` stays per job for shared enrichment, then
   starts one child per *subscribed profile* for eligibility, ranking and notification. Bounded
   concurrency, idempotent by `(job_id, profile_id, content_hash)`.
3. **Realtime**: events carry `workspace_id` (catalogue events carry `source_id`); the event hub
   delivers only to subscribers of the matching workspace or a followed source.
4. **Redis keys** gain a workspace segment: rate limits and quotas per workspace and per user,
   idempotency per user, and cache entries per workspace (the dashboard becomes workspace-specific).

## Identity and access
- Today: GitHub only, owner allowlist. SaaS: open sign-up with GitHub, Google, Microsoft Entra ID
  or an email magic link (Resend); a personal workspace is created on first sign-in, others are
  joined by invitation. Users are keyed by an internal id; provider accounts link to it.
- Roles per workspace: **owner** (billing, delete), **admin** (members, sources), **member**
  (profiles, applications). The API derives the role from `memberships`, never from the token.
- The web→API token keeps `sub` (user) and adds `wid` (the selected workspace); the API verifies
  membership on every request. A workspace switcher lives in the UI.
- The current `OWNER_GITHUB_IDS` becomes a **platform admin** allowlist (support / ops only).

## Plans and limits
| Limit | Free | Pro | Team |
|---|---|---|---|
| Followed boards | 5 | 50 | 500 |
| Fastest polling | 15 min | 2 min | 1 min |
| Seats (incl. pending invites) | 1 | 1 | 25 |
| Manual re-evaluations / day | 20 | 200 | 2,000 |

Enforced in the API (`usage_counters` + Redis) with clear 402/429 problem responses. A
platform admin can always set `workspaces.plan` (sales-led deals, trials).

### Payments
A `BillingProvider` interface (`create_checkout`, `cancel`, `verify_webhook`, `parse_event`)
with two implementations, chosen per workspace by currency/region:

- **Razorpay** (first): Subscriptions API with a plan per tier, hosted checkout, webhooks
  (`subscription.activated` / `.charged` / `.halted` / `.cancelled`) verified with HMAC-SHA256
  of the raw body against the webhook secret.
- **Stripe** (second): Checkout Sessions + Customer Portal, signed webhooks.

Webhooks are the only source of truth for plan changes: stored in `billing_events` (unique
provider event id, so redelivery is idempotent), then applied to `workspaces.plan` /
`subscription_status`. A grace period keeps paid limits for 3 days after a failed charge.
Keys live only in the API's environment (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET`; Stripe equivalents later).

## Migration of existing data
One expand/contract migration: create a "Default" workspace, attach existing users (owners as
workspace owners), profiles and tenant rows to it, add subscriptions for every existing source,
then make `workspace_id` NOT NULL and enable RLS. No downtime; the old single-tenant mode is
simply one workspace.

## Data protection
- Per-workspace export (JSON) and hard delete (cascades tenant tables; catalogue untouched).
- Audit log per workspace; platform-admin access is itself audited.
- PII is limited to member emails/names and profile content; no candidate data in Redis or logs.

## Delivery plan (each step shippable, tests first)
1. ✅ Workspaces, memberships, `workspace_id` + RLS, Default-workspace migration, isolation tests.
2. ✅ Workspace-aware auth (`wid` claim, roles), invitations, workspace switcher, `SIGNUP_POLICY`.
3. ✅ Source deduplication by `(kind, locator)` + per-workspace follow/pause/unfollow; per-profile
   evaluation fan-out. Shared settings (name, intervals) editable by the sole follower or a
   platform admin; follower counts never exposed to tenants.
4. ✅ Workspace-scoped realtime (per-tenant event routing, fail-closed), workspace-keyed cache.
5. ✅ Plans and limits (boards, poll floor, seats, daily re-evaluations; 402 `plan_limit`),
   Razorpay subscriptions (hosted checkout, verified idempotent webhooks, 3-day grace,
   cancel at period end), platform-admin plan override. Stripe remains a second provider.
6. Sign-up flow with Google, Microsoft Entra ID and email magic link; export/delete.

## Risks
- **Fan-out cost**: N profiles × M new jobs evaluations. Mitigated by deterministic hard rules
  first (no LLM), shared enrichment, and per-plan limits.
- **RLS mistakes are silent**: covered by cross-tenant tests on every repository and by running
  the API as a non-`BYPASSRLS` role in CI.
- **Noisy neighbours**: per-workspace quotas and Temporal task-queue fairness keys.
