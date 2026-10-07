# ADR 0001 — Temporal for durable workflows

**Status:** Accepted

## Context
Polling dozens of sources with per-source adaptive intervals, retries, backoff, circuit breaking,
and per-job multi-stage evaluation (some stages calling a paid LLM) requires durability across
worker restarts and an inspectable history.

## Decision
Use Temporal. One long-lived `SourcePollingWorkflow` per source (continue-as-new every 50 polls),
a `SourceDiscoveryWorkflow` per poll, and a `JobEvaluationWorkflow` per job content version.
No Celery, APScheduler, cron chains or Redis queues.

## Consequences
- Timers, retries and backoff are durable; a worker crash resumes mid-pipeline.
- Workflow IDs give idempotency for free (`job-eval-{job}-{hash}`).
- Workflows must be deterministic: all I/O lives in activities, referenced by name.
- Local dev uses the single-binary dev server; production uses Temporal Cloud.
