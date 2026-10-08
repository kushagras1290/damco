# Trade-offs

**Modular monolith + worker over microservices.** One deployable API and one worker share
the domain package. Fewer moving parts; the seams (core package, repositories, services) allow a
later split.

**PostgreSQL for search and vectors.** FTS + trigram + pgvector cover current scale with one
operational system. A dedicated search/vector engine is justified only by measured latency or
recall problems.

**Polling over webhooks.** Most ATS boards offer no webhooks. Adaptive intervals (5–60 min),
conditional GETs (ETag) and backoff keep load polite. Sources with webhooks can be added as an
adapter that signals `poll_now`.

**Full-listing diff to detect closed jobs.** Simple and correct for ATS APIs that return every
open job. Feed sources that only show recent items would close older jobs prematurely; such
sources should be configured with a long interval or a future "incremental" flag.

**One primary profile in the UI, multi-tenant underneath.** Workspaces and PostgreSQL
row-level security are implemented, and the evaluation pipeline already carries both
`workspace_id` and `profile_id`. The remaining limitation is product-facing: the web app
selects the oldest profile in a workspace and has no profile switcher or multi-profile
management screen yet.

**Personal radar first; SaaS second.** The problem this project set out to solve needs one
profile, source polling, eligibility, ranking and notification. Workspaces, plan limits and
the Razorpay adapter were added later as a Phase 2 extension. They increase the security and
operational surface substantially and are deliberately excluded from the primary challenge
demo. If this remained a private personal tool, those features should be removed rather than
operated indefinitely.

**Asymmetric web → API tokens.** Ed25519 keys mean the API can verify but never mint; the
cost is managing a key pair and a rotation procedure (docs/runbook.md).

**Redis-backed rate limiting.** An exact sliding-window log in Redis is shared by every API
instance and costs one Redis round trip per request; if Redis is down, each instance falls back
to its own in-memory window (limits become per instance) rather than failing reads. Cache reads
also bypass Redis. Requests can be slow until the Redis circuit opens, and mutations carrying an
`Idempotency-Key` return 503 because silently losing duplicate protection is unsafe. Volumetric
abuse should still be stopped at the edge (Render / Cloudflare) before it reaches the API.

**Staging snapshots for every fetch.** Costs object-storage writes, but keeps Temporal payloads
tiny and makes every decision replayable from the exact bytes that were fetched.

**LLM degradation instead of failure.** An OpenAI outage must not stop discovery; jobs are
ranked deterministically and can be re-evaluated later (`POST /jobs/{id}/evaluate`).
