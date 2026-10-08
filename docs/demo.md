# Damco reviewer walkthrough (8–10 minutes)

This walkthrough stays on the problem JobPulse was built to solve: discover a posting,
apply eligibility rules, use AI only when the text is ambiguous, produce an explainable
score, and notify once. Workspaces, RLS and billing are Phase 2 extensions and belong in
follow-up questions, not the primary video.

## 1. Prepare a reliable local demo

Requirements: Docker, [uv](https://docs.astral.sh/uv/) 0.12 or newer, Node 24 and pnpm.

```bash
make demo
```

`make demo` fills missing local secrets without printing or overwriting existing values,
starts PostgreSQL, Redis, Temporal, the API, worker and web app, seeds three public boards
plus a synthetic Demo board, and opens the dashboard. Stop it with:

```bash
docker compose down
```

Before recording, verify the stack rather than discovering a failure on camera:

```bash
docker compose ps
curl -fsS http://localhost:8000/health/ready
```

Open these tabs:

1. Dashboard: <http://localhost:3000/dashboard>
2. Jobs: <http://localhost:3000/jobs>
3. Demo source: <http://localhost:3000/sources>
4. Temporal UI: <http://localhost:8233>

Let the stack run for two or three minutes. The Demo board releases three fictional
postings per poll, so the activity feed is visible without depending on a real employer.
All Demo board companies use reserved `.example` domains.

### Choose the honest AI path

Check the System page before recording.

- If it says **intelligence enabled**, show `Backend Engineer, Fleet APIs` (or another job
  with an AI extraction card). Explain which deterministic fact was unknown and which
  structured fact the model supplied.
- If it says **deterministic only**, do not imply a live model call. Show the same job with
  unresolved facts left for review and explain that OpenAI is optional and degradable. Use
  the Redis outage below as the live failure case.

The repository has automated coverage for model success, refusal, invalid schema, timeout
and retry exhaustion. Those tests are evidence of code behaviour, not evidence that a real
provider was exercised in this recording.

## 2. Running order

| Time | Show | Point to make |
|---|---|---|
| 0:00–1:00 | README / face | Personal problem and the five repeated eligibility checks |
| 1:00–2:15 | Dashboard + live activity | A continuous system, not a one-shot script |
| 2:15–4:15 | One job detail | Rules first, AI only for unknowns, stored score components |
| 4:15–5:15 | A clearly ineligible job | AI cannot override a deterministic failure |
| 5:15–6:15 | Source + Temporal UI | Adaptive polling, durable retries and idempotent workflow IDs |
| 6:15–7:15 | Redis outage | One real degradation path and one fail-closed path |
| 7:15–8:45 | Architecture + trade-offs | Why Temporal/PostgreSQL/rules-first; what would change the choices |
| 8:45–10:00 | Known limitations | Specific broken or unverified parts; no hosted-demo claim |

## 3. What to say and click

### Problem (about 60 seconds)

Use the first-person account from the README. Keep it concrete:

> While applying for remote roles from India, I kept reopening separate career pages and
> repeating the same five checks: work model, residency, location, experience and time-zone
> overlap. Generic alerts still sent me US-only jobs and never explained why a role matched.
> I built JobPulse to watch those boards continuously and make the decision trace visible.

Do not introduce SaaS, billing or deployment here. They do not help establish the problem.

### Core pipeline (about 3 minutes)

1. On **Dashboard**, point to the green Live indicator and the activity feed.
2. If no event arrives within 30 seconds, open **Sources → Demo board → Sync now** and
   return to the dashboard.
3. Open a scored job and show, in this order:
   - five rule outcomes and their quoted evidence;
   - the six stored score components;
   - the AI extraction card, following the honest AI path above;
   - the append-only decision trace and source snapshot.
4. Open a US-only, EU-only, on-site or over-senior Demo board job. Point to the failed rule
   and say: "This stops at the deterministic gate. A model is not allowed to turn it into a
   pass."

The key sentence is:

> Deterministic rules own hard eligibility. The model may add structured evidence to an
> unknown rule, but it can never override a fail.

### Durability (about 60 seconds)

Open **Sources → Demo board**, click **Sync now**, then open the newest execution in the
Temporal UI.

Explain the three levels:

- `SourcePollingWorkflow`: one long-running adaptive poller per source, with backoff,
  circuit breaker and continue-as-new.
- `SourceDiscoveryWorkflow`: fetch, normalize, store, then fan out evaluations.
- `JobEvaluationWorkflow`: one job/profile/content decision; eligibility, optional
  enrichment, optional embedding, ranking and notification.

Workflow IDs include the stable decision identity, and notification rows have a unique
dedupe key. A worker restart therefore resumes durable history without sending the same
alert twice.

### Live failure: Redis unavailable (about 60 seconds)

In a terminal:

```bash
docker compose stop redis
curl -s http://localhost:8000/api/v1/system
```

Then load Jobs once and refresh it after the Redis circuit has opened. Explain the exact
boundary:

- reads continue with cache bypass and per-instance in-memory rate limits;
- the first requests can be slow while Redis connection attempts time out;
- writes carrying `Idempotency-Key` return 503 rather than silently lose duplicate
  protection;
- PostgreSQL remains the system of record, so no durable data is lost.

Restore Redis immediately after the demonstration:

```bash
docker compose start redis
```

This is both the failure demo and one item for the final "what's broken" section.

### Trade-offs (about 90 seconds)

Use [tradeoffs.md](tradeoffs.md) or the ADR list. For each choice, state the cost and the
condition that would change it:

- **Temporal over cron/Celery:** more infrastructure, justified by long-running polling,
  durable sleeps and replay. A single short pipeline would not justify it.
- **PostgreSQL search/vector/realtime over separate services:** one consistency and backup
  boundary at current scale. Measured recall or latency problems would justify a dedicated
  search system.
- **Rules before AI:** cheaper and auditable, but the rules need maintenance. User outcome
  data showing systematic false negatives would justify revisiting the boundary.
- **Phase 2 RLS workspaces:** strong shared-database isolation, but much more surface area
  than a personal radar needs. A permanently personal tool should delete that extension;
  contractual isolation could require database-per-tenant instead.

### What's broken (about 60 seconds)

Say these plainly:

1. There is no hosted read-only demo yet; cloud manifests are present but unverified.
2. Real OpenAI, Resend, Razorpay, Google and Microsoft credentials have not all been
   exercised end to end; mock coverage is not live-provider proof.
3. Full-listing closure detection is unsafe for recent-items-only feeds.
4. Redis failover weakens rate limiting across replicas and can make the first requests slow.
5. The UI exposes only one primary candidate profile per workspace.

End with what is verified: the local vertical slice, deterministic behaviour, integration
tests and failure handling. Do not claim production deployment, adoption or provider
reliability.

## 4. Keep Phase 2 out of the primary video

If asked, the workspace/RLS, provider sign-in, plan-limit and Razorpay code is available as
evidence of further engineering. Frame it accurately:

> I built the personal radar first. I later explored how to share the expensive catalogue
> safely across users. That became a separate Phase 2 extension, and in hindsight it is more
> scope than the challenge needed.

Do not spend primary-demo time on checkout, invitations or the workspace switcher. They
make the submission look less focused and none is needed to demonstrate the core decision
pipeline.

## 5. Troubleshooting

| Symptom | Check |
|---|---|
| Indicator shows **Polling** | `docker compose logs api`; `LISTEN` needs a direct `DATABASE_LISTEN_URL` when the normal database URL uses a transaction pooler. |
| No Demo board activity | `docker compose logs worker`; confirm `DEMO_MODE=true`, then run `make seed-demo`. |
| No AI extraction | Check the System page. No key means deterministic-only mode by design. |
| 429 response | Wait for `RateLimit-Reset`; local limits come from `RATE_LIMIT_*`. |
| A write returns 503 while Redis is stopped | Expected fail-closed idempotency behaviour; restore Redis. |
