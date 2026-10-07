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
| Ephemeral state | Render Key Value (Redis) | Shared rate limits, idempotency keys, 5 s response cache; API only; safe to flush ([ADR 0007](adr/0007-redis-for-ephemeral-shared-state.md)) |
| Realtime | PostgreSQL LISTEN/NOTIFY → SSE | One LISTEN connection per API process via `DATABASE_LISTEN_URL` (direct, unpooled Neon URL) |

## First-time setup checklist

1. **Keys**: `make keys` → `API_JWT_PRIVATE_JWK` to Vercel, `API_JWT_JWKS` to Render (API).
2. **Owners**: `gh api users/<login> --jq .id` → `OWNER_GITHUB_IDS` on **both** Vercel and Render
   (the API's copy is authoritative; the web copy only gates sign-in and UI).
3. **GitHub App (sign-in)**: add callback `https://<web-domain>/api/auth/callback/github` to the
   existing app (no permissions needed; see README for the pre-filled registration link); set
   `AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET`, `AUTH_SECRET` (≥ 32 chars), `AUTH_URL=https://<web-domain>`.
4. **Web**: `ENVIRONMENT=production`, `API_BASE_URL=https://<api-domain>`. Production config
   validation refuses to serve with http URLs, missing OAuth or no owners.
5. **R2 lifecycle rule**: expire objects under `staging/` after 7 days (they are only needed
   while a discovery run is in flight). Keep `raw/` and `pages/` (audit + replay).
6. **Spend guards**: set `OPENAI_*_PRICE_PER_MILLION`, `OPENAI_DAILY_BUDGET_USD` and/or
   `OPENAI_DAILY_REQUEST_LIMIT` (default 2000/day).
7. **Redis + realtime**: `REDIS_URL` is wired from the Key Value instance by `render.yaml`
   (the API refuses to start in production without it). Set `DATABASE_LISTEN_URL` to the
   *direct* Neon URL, otherwise realtime is disabled and the UI falls back to polling.
8. **Edge protection**: keep Vercel Firewall / WAF rate limiting for volumetric abuse. The
   API's limiter is shared via Redis and keyed per owner (`github:<id>`), per anonymous
   visitor (`visitor:<hmac>`, minted by the web tier from Vercel's client IP) or per IP.
9. **Deploys**: add the `production` environment secrets listed in the README, then set the
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
| `circuit_open{name="redis"} == 1` | Redis unreachable; limits per instance, cache bypassed, keyed writes 503 | Check Key Value status; clients retry keyed writes automatically |
| `rate_limit_backend_fallbacks_total` rising | Decisions served by the local fallback limiter | Same as above |
| `response_cache_total{result="hit"}` ratio low | Cache not absorbing dashboard load | Check TTL (`CACHE_TTL_SECONDS`) and Redis health |

## Backups and data retention

- Neon point-in-time restore covers the database; test a restore quarterly.
- R2: `raw/` and `pages/` are immutable evidence for decisions - keep. `staging/` expires (above).
- Decision tables are append-only by design; prune `workflow_runs` older than 90 days if needed.

## Incident quick reference

- **All sources failing** → check outbound allowlist (`OUTBOUND_ALLOWED_HOSTS`) and DNS.
- **401s from the API** → `kid` mismatch between web private key and API JWKS, or clock skew > 30 s.
- **Owner sees read-only UI** → their numeric id missing from `OWNER_GITHUB_IDS` on the API.
- **UI shows "Polling" instead of "Live"** → API logged `events.disabled` (pooled URL without
  `DATABASE_LISTEN_URL`) or the LISTEN connection is reconnecting (`events.listener_disconnected`).
- **Burst of 503 `idempotency_unavailable`** → Redis down; unkeyed reads/writes still work.
- **Workflow task failures after deploy** → non-deterministic workflow change; roll back the
  worker, add `workflow.patched`, redeploy.
