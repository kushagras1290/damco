# Live demo walkthrough (10 minutes)

## Start it

```bash
make demo        # = uv run python scripts/demo.py
```

The script fills any missing secrets in `.env` (never overwriting existing values, never
printing them), turns on `DEMO_MODE`, starts the stack (PostgreSQL, Redis, Temporal, API,
worker, web), seeds three real public job boards plus a **Demo board**, and opens the
dashboard. Requirements: Docker, uv, ~3 GB free disk. Stop with `docker compose down`.

The Demo board contains fictional companies (`*.example` domains). It releases a few new
postings every minute so you can watch the realtime pipeline without waiting for real
companies to post jobs. It cannot be enabled in production.

## What to show

1. **Dashboard → Live activity.** The green **Live** dot means the browser holds a
   Server-Sent Events stream. About once a minute you will see, in order:
   *checking for new jobs → fetched N postings → 3 new jobs → Scored / Filtered out → Strong
   match*. A strong match also raises a toast. Counters and charts refresh from these events
   (no polling while the stream is live).
2. **Jobs → a demo job.** Every decision is explained: which hard rules passed or failed,
   the score breakdown, matched and missing skills.
3. **Sources.** A source shows a pulsing *syncing* badge while it is being fetched; health
   shows backoff and the circuit breaker after repeated failures.
4. **Runs / Temporal UI (http://localhost:8233).** Each poll is a durable workflow; kill the
   worker (`docker compose restart worker`) and it resumes where it left off.
5. **Production behaviours** (from a terminal):

   ```bash
   # Rate limit headers on every response (per user / per visitor / per IP, shared via Redis)
   curl -si localhost:8000/api/v1/jobs?limit=1 | grep -i ratelimit

   # Stop Redis: the API keeps serving (per-instance limits, cache bypass), System page shows it
   docker compose stop redis && curl -s localhost:8000/api/v1/system | jq '.dependencies'
   docker compose start redis

   # The raw event stream
   curl -N localhost:8000/api/v1/events
   ```

6. **Owner features.** Sign in with GitHub (see README → Owner access) to add sources,
   trigger a sync, edit the eligibility policy, and track applications. Writes carry an
   `Idempotency-Key`, so a retried request never creates duplicates.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Indicator shows **Polling** | Realtime is off: check `docker compose logs api` for `events.disabled` (pooled DB URL without `DATABASE_LISTEN_URL`). |
| No demo activity | `docker compose logs worker`; ensure `.env` has `DEMO_MODE=true`, then `make seed-demo`. |
| 429 responses | You hit a rate limit; limits are in `.env` (`RATE_LIMIT_*`). |
