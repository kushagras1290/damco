# JobPulse

Event-driven AI job opportunity radar: discovers jobs from ATS boards and feeds, applies
**deterministic hard-eligibility rules**, uses an LLM **only for ambiguous facts**, ranks
matches with an **explainable** score, and notifies you — with every decision auditable and
replayable.

```
Sources (Greenhouse · Lever · Ashby · RSS · JSON · HTML)
   │  SourcePollingWorkflow (adaptive interval, backoff, circuit breaker)
   ▼
SourceDiscoveryWorkflow: Fetch → Normalize → Store (3-layer dedup, versions, R2 snapshots)
   │  one child per new/changed job
   ▼
JobEvaluationWorkflow: Eligibility → Enrichment → Embedding → Ranking → Notification
   │
   ▼
PostgreSQL 18 (pgvector · pg_trgm · FTS)  ──►  FastAPI  ──►  Next.js dashboard
```

## What is in the box

| Area | Implementation |
|---|---|
| Domain core | `packages/python` — pure, framework-free: adapters, normalizer, eligibility engine, ranking, OpenAI client, notifications, polling policy |
| API | `apps/api` — FastAPI, SQLAlchemy 2 async + psycopg 3, Alembic, structlog, Prometheus, OTel, Sentry |
| Worker | `apps/worker` — Temporal workflows/activities, continue-as-new polling loops |
| Web | `apps/web` — Next.js 16, React 19, Tailwind 4, shadcn-style UI, TanStack Query/Table v9, Zustand, RHF + Zod, Recharts, Auth.js (GitHub) |
| Data | PostgreSQL 18: UUIDv7 keys, pgvector HNSW, `pg_trgm` GIN, generated `tsvector` + GIN |
| Ops | Docker multi-stage (non-root, read-only), Compose, GitHub Actions CI/CD, Render + Vercel + Neon + Temporal Cloud + R2 |

## Quick start (local)

Requirements: Docker, [uv](https://docs.astral.sh/uv/) ≥ 0.12, Node 24 + pnpm.

```bash
cp .env.example .env            # set API_JWT_SECRET and AUTH_SECRET (≥ 32 chars)
docker compose up --build -d    # Postgres, Temporal (+UI), migrations, API, worker, web
make seed                       # demo profile + three public job boards
```

- Dashboard → http://localhost:3000 (read-only public demo unless signed in as owner)
- API docs → http://localhost:8000/docs
- Temporal UI → http://localhost:8233

**Owner access:** create a GitHub OAuth app (callback `http://localhost:3000/api/auth/callback/github`),
set `AUTH_GITHUB_ID`, `AUTH_GITHUB_SECRET` and `OWNER_GITHUB_LOGINS=<your-login>`.

**AI enrichment** is optional. Without `OPENAI_API_KEY`, JobPulse runs in deterministic-only
mode: hard rules and keyword skill matching still work; unknown rules remain flagged for review.

## Development

```bash
make install     # uv sync + pnpm install
make check       # ruff, mypy --strict, eslint, tsc, pytest, vitest
make test        # integration tests start PostgreSQL via Testcontainers
make e2e         # Playwright against the running stack
```

Set `JOBPULSE_TEST_DATABASE_URL` to reuse an existing Postgres instead of Testcontainers.

## API

Versioned REST under `/api/v1`: `jobs`, `jobs/{id}` (full decision trace), `jobs/{id}/snapshot`,
`jobs/{id}/evaluate`, `jobs/{id}/applications`, `sources` (+ `PATCH`, `/sync`), `runs`, `profile`,
`applications`, `decisions`, `dashboard`, `system`, `me`. Probes: `/health/live`, `/health/ready`, `/metrics`.

Writes require the `OWNER` role (JWT minted server-side by the web app). Anonymous callers are
`PUBLIC_DEMO` (read-only). Errors are RFC 9457 `application/problem+json`.

## How a decision is made

1. **Deterministic eligibility** — work model, residency/exclusion, location, experience,
   timezone. Each rule yields `pass` / `fail` / `unknown` with quoted evidence.
2. **AI enrichment** (only if some rule is `unknown`) — OpenAI Responses API with a strict
   JSON schema; cheap model first, reasoning model only when confidence is low. AI may resolve
   `unknown` rules but **can never override a deterministic `fail`**.
3. **Ranking** — `0.30 skill + 0.20 semantic + 0.15 role + 0.15 seniority + 0.10 location/tz +
   0.10 freshness`; missing components are re-weighted transparently; every component is stored.
4. **Notification** — once per job content version per channel (idempotent dedupe key).

## Documentation

- [Architecture](docs/architecture/overview.md)
- [ADRs](docs/adr/)
- [Security](docs/security.md)
- [Failure modes](docs/failure-modes.md)
- [Trade-offs](docs/tradeoffs.md)

## License

MIT — see [LICENSE](LICENSE).
