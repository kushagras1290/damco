# ADR 0007 — Redis for ephemeral shared state; PostgreSQL LISTEN/NOTIFY for realtime

**Status:** Accepted · amends [ADR 0003](0003-postgres-as-the-only-datastore.md)

## Context
Turning JobPulse into a live product added three needs that per-process memory cannot meet
once the API runs as more than one instance:

1. **Rate limits** must be enforced across all API instances (otherwise N replicas silently
   multiply every limit by N).
2. **Idempotency keys** must be visible to every instance, or a client retry that lands on a
   different replica creates a duplicate.
3. **Hot aggregates** (dashboard, system status) are read by every open browser tab and
   should not each cost a set of database aggregations.

It also needed **realtime delivery** of pipeline events (discovered, evaluated, matched,
alerted) to browsers.

## Decision
- **Redis** (local: `redis:8.8.3-alpine`; production: Render Key Value) holds only
  *ephemeral, TTL-bound* state: sliding-window rate-limit logs (`jp:rl:*`), idempotency
  records (`jp:idem:*`, 24 h) and response cache entries (`jp:cache:*`, 5 s). Nothing in Redis
  is a source of truth; it can be flushed at any time. Eviction policy `volatile-lru`.
- **PostgreSQL stays the only system of record** (ADR 0003 still holds for durable data).
- **Realtime events use PostgreSQL `LISTEN/NOTIFY`**, not Redis pub/sub: producers call
  `pg_notify` *inside the same transaction* as the data change, so an event is delivered if
  and only if the change commits. Each API process holds one `LISTEN` connection and fans out
  to browsers over **Server-Sent Events** (`GET /api/v1/events`).

## Failure behaviour (designed, and covered by tests)
| Redis state | Rate limiting | Cache | Idempotency-keyed writes | Unkeyed requests |
|---|---|---|---|---|
| healthy | shared, exact sliding window | 5 s TTL | replay / 409 / 422 semantics | normal |
| down | per-instance fallback limiter | bypassed (DB directly) | **503 + Retry-After** (never a silent duplicate) | normal |

A shared **circuit breaker** stops every request from paying Redis connect timeouts during an
outage: after two failures all Redis features take the fallback path immediately and one probe
per 5 s checks for recovery (`circuit_open{name="redis"}` gauge).

The API refuses to start in production without `REDIS_URL`; the worker never uses Redis and
does not require it.

## Rejected alternatives
- **Redis pub/sub or Streams for events** — would publish before (or without) the database
  commit unless we added an outbox; NOTIFY gives commit-coupled delivery for free.
- **Rate limiting in PostgreSQL** — a write per request on the primary database.
- **WebSockets** — events are one-way server→browser; SSE works through the existing
  same-origin proxy, auto-reconnects, and needs no extra protocol handling.
- **In-memory limits only** — wrong as soon as there are two replicas.

## Consequences
- One more managed dependency (small, stateless in practice).
- NOTIFY payloads are capped (we stay under 7 KB and send ids/summaries; clients refetch
  details). Events are best-effort: a browser that was disconnected refetches all queries on
  reconnect instead of replaying missed events.
- Behind a transaction pooler (Neon `-pooler`), `LISTEN` needs a direct connection:
  `DATABASE_LISTEN_URL`. Without it realtime is disabled and the UI falls back to polling.
