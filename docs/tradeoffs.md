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

**Single primary profile.** v1 is single-tenant (an owner + read-only demo). Tables already
carry `profile_id`, so multi-profile scoring is additive.

**Asymmetric web → API tokens.** Ed25519 keys mean the API can verify but never mint; the
cost is managing a key pair and a rotation procedure (docs/runbook.md).

**In-process rate limiting.** Correct for one API instance; with more instances, rate-limit at
the edge (Render / Cloudflare).

**Staging snapshots for every fetch.** Costs object-storage writes, but keeps Temporal payloads
tiny and makes every decision replayable from the exact bytes that were fetched.

**LLM degradation instead of failure.** An OpenAI outage must not stop discovery; jobs are
ranked deterministically and can be re-evaluated later (`POST /jobs/{id}/evaluate`).
