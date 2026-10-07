# Production runbook

## Topology

| Component | Platform | Notes |
|---|---|---|
| Web (Next.js) | Vercel | Signs Ed25519 access tokens; holds `API_JWT_PRIVATE_JWK`, `AUTH_SECRET`, GitHub OAuth secrets |
| API (FastAPI) | Render web service | `python -m jobpulse.serve`; honours `$PORT`; holds only public keys (`API_JWT_JWKS`) |
| Worker (Temporal) | Render background worker | Long-running polling + evaluation workflows |
| Database | Neon PostgreSQL 18 | Pooled (`-pooler`) endpoint auto-detected: prepared statements off, `SET LOCAL statement_timeout` per transaction |
| Workflows | Temporal Cloud | API key + TLS |
| Snapshots | Cloudflare R2 | `raw/`, `pages/` (content-addressed), `staging/` (transient) |

## First-time setup checklist

1. **Keys**: `make keys` → `API_JWT_PRIVATE_JWK` to Vercel, `API_JWT_JWKS` to Render (API).
2. **Owners**: `gh api users/<login> --jq .id` → `OWNER_GITHUB_IDS` on **both** Vercel and Render
   (the API's copy is authoritative; the web copy only gates sign-in and UI).
3. **GitHub OAuth app**: callback `https://<web-domain>/api/auth/callback/github`; set
   `AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET`, `AUTH_SECRET` (≥ 32 chars), `AUTH_URL=https://<web-domain>`.
4. **Web**: `ENVIRONMENT=production`, `API_BASE_URL=https://<api-domain>`. Production config
   validation refuses to serve with http URLs, missing OAuth or no owners.
5. **R2 lifecycle rule**: expire objects under `staging/` after 7 days (they are only needed
   while a discovery run is in flight). Keep `raw/` and `pages/` (audit + replay).
6. **Spend guards**: set `OPENAI_*_PRICE_PER_MILLION`, `OPENAI_DAILY_BUDGET_USD` and/or
   `OPENAI_DAILY_REQUEST_LIMIT` (default 2000/day).
7. **Edge protection**: enable Vercel Firewall / WAF rate limiting for anonymous traffic;
   the API's limiter is per instance and keys authenticated callers by verified identity.
8. **Deploys**: add the `production` environment secrets listed in the README, then set the
   repository variable `DEPLOY_ENABLED=true`.

## Deploy order (automated by `deploy.yml`)

migrate → API → worker → web → smoke tests. Consequences:

- **Migrations must be backward compatible** with the currently running code (expand →
  migrate data → contract in a later release). Never drop/rename a column the old API reads.
- **Temporal workflow code must stay replay-compatible.** Polling workflows are long-lived;
  changing workflow logic (not activities) requires `workflow.patched("change-id")` guards, or
  a new workflow type name. Activities can change freely. `SourcePollingWorkflow`
  continues-as-new every 50 polls, so old histories age out within days.

## Key and secret rotation

| Secret | Procedure |
|---|---|
| API signing key | `make keys` (new `kid`) → **append** its public key to `API_JWT_JWKS`, deploy API → set new `API_JWT_PRIVATE_JWK` on web → after 5 min (token TTL) remove the old public key |
| `AUTH_SECRET` (session cookies) | Set new value as `AUTH_SECRET`, move the old one to `AUTH_SECRET_1`; remove after the 8 h session lifetime |
| GitHub OAuth secret | Rotate in GitHub, update Vercel env, redeploy web |
| Revoke an owner | Remove the id from `OWNER_GITHUB_IDS` on API (+ web). Effective on the next request - roles are re-evaluated per request, never trusted from tokens |
| `WEBHOOK_SIGNING_SECRET` | Coordinate with receivers; they verify `X-JobPulse-Signature` |

## Alerts worth wiring (Prometheus metric → action)

| Metric | Meaning | Action |
|---|---|---|
| `source_payload_near_limit_total` | A board passed 75 % of `OUTBOUND_MAX_RESPONSE_BYTES` | Raise the limit before the source hard-fails |
| `source_suspicious_listings_total` | Listing shrank > 50 %; closures skipped | Check the source; closures resume on the next healthy poll |
| `source_failures_total` rising / circuit open | Upstream broken, token wrong, or robots disallow | Inspect `last_error` on `/sources/{id}` |
| `llm_budget_exhausted_total` | Daily LLM cap hit; enrichment degraded to deterministic | Raise the cap or accept; re-evaluate jobs later |
| `notifications_sent_total{outcome="failed"}` | Email/webhook failures | Check Resend status / webhook receiver |
| `http_requests_total{status="5xx"}` | API errors | Sentry + logs by `request_id` |

## Backups and data retention

- Neon point-in-time restore covers the database; test a restore quarterly.
- R2: `raw/` and `pages/` are immutable evidence for decisions - keep. `staging/` expires (above).
- Decision tables are append-only by design; prune `workflow_runs` older than 90 days if needed.

## Incident quick reference

- **All sources failing** → check outbound allowlist (`OUTBOUND_ALLOWED_HOSTS`) and DNS.
- **401s from the API** → `kid` mismatch between web private key and API JWKS, or clock skew > 30 s.
- **Owner sees read-only UI** → their numeric id missing from `OWNER_GITHUB_IDS` on the API.
- **Workflow task failures after deploy** → non-deterministic workflow change; roll back the
  worker, add `workflow.patched`, redeploy.
