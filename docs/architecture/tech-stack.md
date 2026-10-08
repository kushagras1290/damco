# JobPulse — Production Tech Stack

> Event-driven AI job opportunity radar for discovering, qualifying, ranking, and notifying users about relevant jobs with explainable decisions.

> **Historical planning document.** This captured the broad design space before the runnable
> vertical slice. It is intentionally not the source of truth for delivered scope or current
> behaviour, and some provider/deployment sections are unverified. Start with the
> [README](../../README.md), [architecture overview](overview.md),
> [trade-offs](../tradeoffs.md), and [ADR 0004](../adr/0004-implementation-deviations.md).

---

## 1. Architecture Goals

JobPulse is designed around the following principles:

- Event-driven where possible
- Durable background workflows
- Deterministic rules for hard eligibility constraints
- LLMs only for ambiguous or semantic reasoning
- PostgreSQL as the primary data platform
- Strong auditability and replayability
- Explainable ranking decisions
- Minimal operational overhead
- Production-grade observability and security
- No unnecessary distributed-system complexity

---

## 2. High-Level Architecture

```text
                   ┌─────────────────────────────┐
                   │       Source Adapters       │
                   │ Greenhouse / Lever / Ashby │
                   │ RSS / Careers / APIs / Web │
                   └──────────────┬──────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │    Temporal Workflow     │
                    │ Scheduling / retries /   │
                    │ backoff / orchestration  │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                    ┌──────────────────────────┐
                    │     Ingestion Layer      │
                    │ HTTPX / Playwright       │
                    │ Parser / Normalizer      │
                    └─────────────┬────────────┘
                                  │
                                  ▼
                  ┌──────────────────────────────┐
                  │         PostgreSQL           │
                  │ jobs / sources / runs /      │
                  │ profiles / decisions         │
                  │ pgvector / FTS / pg_trgm     │
                  └──────────────┬───────────────┘
                                 │
                      ┌──────────▼──────────┐
                      │ Eligibility Engine │
                      │ deterministic rules│
                      └──────────┬──────────┘
                                 │
                            PASS │
                                 ▼
                    ┌────────────────────────┐
                    │   AI Enrichment Layer  │
                    │ OpenAI Structured JSON │
                    │ embeddings / reasoning │
                    └────────────┬───────────┘
                                 │
                                 ▼
                    ┌────────────────────────┐
                    │     Ranking Engine     │
                    │ deterministic + AI     │
                    └────────────┬───────────┘
                                 │
                       ┌─────────┴────────┐
                       ▼                  ▼
                Notifications        Dashboard
                Email/Webhook         Next.js
```

---

## 3. Backend

### Core

- Python 3.14
- FastAPI
- Pydantic v2
- SQLAlchemy 2.x
- psycopg 3 async
- Alembic
- pydantic-settings
- structlog
- HTTPX
- uv

### Responsibilities

The backend owns:

- API endpoints
- user/job/source domain models
- database access
- eligibility policies
- ranking logic
- job/source management
- workflow triggering
- notification configuration
- audit history

---

## 4. Workflow Orchestration

### Temporal

Use Temporal for:

- durable workflows
- scheduled polling
- retries
- exponential backoff
- timeouts
- workflow history
- cancellation
- long-running ingestion
- idempotent job processing
- recovery after worker restarts

### Main Workflows

```text
SourceDiscoveryWorkflow
    ├── FetchJobsActivity
    ├── NormalizeJobsActivity
    └── StoreJobsActivity

JobEvaluationWorkflow
    ├── EligibilityActivity
    ├── EnrichmentActivity
    ├── EmbeddingActivity
    ├── RankingActivity
    └── NotificationActivity
```

### Explicit Non-Choices

Do not use:

- Celery
- APScheduler
- ad-hoc cron chains
- Redis as the primary job queue

Temporal owns workflow durability.

---

## 5. Database

### PostgreSQL 18

Primary database for all transactional and analytical application state.

### Extensions

- pgvector
- pg_trgm
- PostgreSQL full-text search
- native UUIDv7 where practical

### Core Entities

```text
User
Profile
Company
Source
SourceCheckpoint
Job
JobVersion
RawSnapshot
EligibilityDecision
JobIntelligence
MatchScore
Notification
Application
WorkflowRun
AuditEvent
```

### Why PostgreSQL

The data is strongly relational and requires:

- transactions
- unique constraints
- joins
- traceable state
- audit history
- vector search
- text search
- fuzzy matching

MongoDB is not needed.

---

## 6. Vector Search

### pgvector

Use embeddings for:

- profile ↔ job-description similarity
- skill similarity
- role similarity
- historical-interest similarity

No external vector database in v1.

Do not use:

- Pinecone
- Qdrant
- Weaviate

unless scale or operational requirements later justify a dedicated vector service.

---

## 7. Search Strategy

### Exact / Filter Search

PostgreSQL B-tree indexes for:

- company
- source
- published_at
- location
- remote_policy
- seniority
- eligibility status
- workflow state

### Full-Text Search

Use:

- tsvector
- GIN indexes

For search terms such as:

- Python
- LangGraph
- RAG
- Agentic AI
- FastAPI

### Fuzzy Search

Use `pg_trgm` for near-duplicate titles.

Examples:

```text
Senior AI Engineer
Sr AI Engineer
Senior Artificial Intelligence Engineer
```

### Semantic Search

Use pgvector.

---

## 8. Raw Source Storage

### Cloudflare R2

Store immutable source snapshots.

```text
raw/
  greenhouse/
    company/
      job-id/
        timestamp.json

pages/
  company/
    job-id/
      timestamp.html
```

Database stores:

```text
snapshot_key
snapshot_hash
fetched_at
content_type
source_id
job_id
```

This enables:

- debugging
- decision replay
- parser regression analysis
- auditability

---

## 9. Source Adapters

Define a source contract:

```python
class JobSource(Protocol):
    async def discover(
        self,
        checkpoint: SourceCheckpoint,
    ) -> list[RawJob]: ...
```

Implement adapters such as:

- GreenhouseSource
- LeverSource
- AshbySource
- RSSSource
- GenericJSONSource
- StaticHTMLSource
- DynamicHTMLSource

---

## 10. Ingestion Tooling

Use:

- HTTPX — APIs and static pages
- selectolax — fast HTML parsing
- feedparser — RSS / Atom
- Playwright — dynamic JS sites only
- nh3 — HTML sanitization
- SHA-256 — canonical content hashing
- urllib / custom URL normalization

### Rule

Do not launch Playwright unless HTTP fetching cannot retrieve the required data.

---

## 11. Adaptive Polling

For sources without webhooks:

```text
active source
    ↓
shorter polling interval

quiet source
    ↓
longer polling interval

HTTP 429 / throttling
    ↓
exponential backoff

repeated failure
    ↓
circuit breaker

new job detected
    ↓
temporarily increase polling rate
```

Temporal manages delays and retries.

Example interval range:

```text
5 min
10 min
15 min
30 min
60 min
```

---

## 12. Deduplication

Use three layers.

### Layer 1 — Source Identity

```text
source_id + external_job_id
```

Database constraint:

```sql
UNIQUE(source_id, external_job_id)
```

### Layer 2 — Canonical URL

Normalize:

- query params
- trailing slashes
- protocol aliases
- tracking parameters

### Layer 3 — Fingerprint

```text
SHA256(
    company_domain
    + normalized_title
    + normalized_location
)
```

Use fuzzy matching as a secondary signal.

---

## 13. AI Layer

### OpenAI Responses API

Use Structured Outputs with strict JSON schemas.

Do not rely on unstructured text parsing.

Example schema:

```python
class JobIntelligence(BaseModel):
    remote_policy: RemotePolicy
    permitted_countries: list[str]
    excluded_countries: list[str]

    minimum_experience: float | None
    maximum_experience: float | None

    required_skills: list[str]
    preferred_skills: list[str]

    seniority: Seniority

    sponsorship_available: bool | None
    timezone_requirements: list[str]

    confidence: float
```

---

## 14. Model Strategy

Models must be configuration-driven.

```env
OPENAI_CLASSIFICATION_MODEL=
OPENAI_REASONING_MODEL=
OPENAI_EMBEDDING_MODEL=
```

### Routing

```text
cheap/small model
    ↓
routine structured extraction

larger reasoning model
    ↓
only ambiguous cases
```

### Principle

Do not use an LLM for deterministic work.

---

## 15. Eligibility Engine

Hard eligibility rules run before semantic scoring.

Example policy:

```yaml
allowed_locations:
  - India
  - Worldwide

allowed_work_models:
  - remote

allowed_timezones:
  - IST
  - GMT
  - BST
  - CET
  - EET

experience:
  min: 3
  max: 6

excluded_regions:
  - US-only
  - Canada-only
```

Pipeline:

```text
Job Description
      │
      ▼
Deterministic Eligibility
      │
  ┌───┴────┐
  │        │
FAIL      PASS
  │        │
Reject     ▼
        AI Enrichment
```

### Important Rule

LLMs cannot override explicit hard constraints.

Example:

```text
"Candidates must reside in the United States."
```

must remain ineligible even if semantic match is high.

---

## 16. Ranking Engine

Suggested scoring model:

```text
Final Match Score
=
0.30 skill_match
+
0.20 semantic_similarity
+
0.15 role_similarity
+
0.15 seniority_fit
+
0.10 location_timezone_fit
+
0.10 freshness
```

Hard eligibility always wins:

```text
eligible = false
→ score not actionable
```

All score components must be persisted for explainability.

---

## 17. Frontend

### Core

- Next.js 16
- React 19
- TypeScript
- Tailwind CSS 4
- shadcn/ui

### State and Forms

- TanStack Query
- Zustand
- React Hook Form
- Zod

### Data / UI

- TanStack Table
- Recharts
- Lucide
- date-fns

---

## 18. Frontend Routes

```text
/dashboard
/jobs
/jobs/:id
/sources
/sources/:id
/applications
/profile
/runs
/decisions
/system
```

---

## 19. Job Explainability UI

Every job should expose:

```text
Title
Company
Source
Published At

Eligibility
- Remote
- Country eligibility
- Experience fit
- Timezone fit

Skill Match
- matched skills
- missing skills

Decision Trace
- deterministic rules
- AI extraction
- semantic similarity
- final score

Source Snapshot
- original fetched content
```

Never show only a magical percentage.

---

## 20. Authentication

### Auth.js

Use GitHub OAuth for the challenge.

Roles:

```text
PUBLIC_DEMO
OWNER
```

### PUBLIC_DEMO

Read-only access.

### OWNER

Can:

- add source
- disable source
- modify profile
- re-run job
- trigger ingestion
- configure notifications

Backend authorization remains server-side.

---

## 21. Notifications

Provider contract:

```python
class NotificationProvider(Protocol):
    async def send(
        self,
        notification: Notification,
    ) -> None: ...
```

Initial providers:

- Resend email
- Webhook

Potential later integrations:

- Telegram
- Slack
- Discord
- WhatsApp

---

## 22. API Design

Use REST.

Do not use GraphQL in v1.

Suggested endpoints:

```text
GET    /api/v1/jobs
GET    /api/v1/jobs/{id}

GET    /api/v1/sources
POST   /api/v1/sources
PATCH  /api/v1/sources/{id}

POST   /api/v1/sources/{id}/sync

GET    /api/v1/runs
GET    /api/v1/runs/{id}

GET    /api/v1/profile
PATCH  /api/v1/profile

GET    /api/v1/applications
POST   /api/v1/jobs/{id}/applications

GET    /health/live
GET    /health/ready
GET    /metrics
```

Version APIs from day one:

```text
/api/v1/
```

---

## 23. Observability

### Structured Logging

Use `structlog`.

Example:

```json
{
  "event": "job.evaluated",
  "job_id": "uuid",
  "workflow_id": "temporal-id",
  "source_id": "uuid",
  "eligible": true,
  "score": 0.91,
  "duration_ms": 281
}
```

### Distributed Tracing

Use OpenTelemetry.

Trace:

```text
Temporal
  ↓
HTTP Fetch
  ↓
Normalization
  ↓
Database
  ↓
OpenAI
  ↓
Notification
```

### Metrics

Expose Prometheus-compatible metrics.

```text
jobs_discovered_total
jobs_rejected_total
jobs_matched_total

source_fetch_duration_seconds
source_failures_total

llm_requests_total
llm_cost_usd_total

notifications_sent_total
job_processing_duration_seconds
```

### Error Monitoring

Use Sentry for:

- backend exceptions
- frontend exceptions
- failed integrations

---

## 24. Security

### CI Security Checks

- Gitleaks
- Semgrep
- CodeQL
- Trivy
- pip-audit
- npm audit

### Runtime Controls

- strict URL allowlisting
- SSRF protection
- outbound request validation
- request size limits
- request timeouts
- rate limiting
- HTML sanitization
- parameterized SQL
- OAuth/JWT validation
- CORS allowlists
- secure HTTP headers
- environment-based secrets
- no hardcoded credentials
- robots.txt compliance

### Critical Risk

Because JobPulse fetches external URLs, SSRF protection is mandatory.

---

## 25. Testing

### Backend

- pytest
- pytest-asyncio
- pytest-cov
- Hypothesis
- respx
- Testcontainers
- Temporal test environment

### Frontend

- Vitest
- React Testing Library
- Playwright

### Test Pyramid

```text
unit
 ↓
parser contract tests
 ↓
repository tests
 ↓
workflow tests
 ↓
API integration tests
 ↓
end-to-end tests
```

### Source Fixtures

```text
tests/fixtures/greenhouse/
tests/fixtures/lever/
tests/fixtures/ashby/
```

Frozen fixtures ensure source adapters fail loudly when parsing logic breaks.

---

## 26. CI/CD

### GitHub Actions

Pull Request:

```text
lint
typecheck
unit tests
integration tests
frontend tests
security scans
container build
```

Main branch:

```text
test
  ↓
build
  ↓
push image
  ↓
database migration
  ↓
deploy API
  ↓
deploy worker
  ↓
deploy frontend
  ↓
smoke tests
```

Use Dependabot or Renovate for dependency updates.

---

## 27. Containers

Use Docker with multi-stage builds.

Images:

```text
jobpulse-api
jobpulse-worker
jobpulse-web
```

Requirements:

- non-root user
- minimal runtime image
- health checks
- no secrets baked into images
- read-only filesystem where practical

---

## 28. Local Development

Use Docker Compose.

```bash
docker compose up
```

Local services:

```text
PostgreSQL
Temporal
Temporal UI
FastAPI
Temporal Worker
Next.js
```

Python package manager:

```text
uv
```

JavaScript package manager:

```text
pnpm
```

---

## 29. Production Deployment

### Frontend

Vercel

### API

Render Web Service

### Worker

Render Background Worker

### Database

Neon PostgreSQL

### Workflow Engine

Temporal Cloud

### Object Storage

Cloudflare R2

### Email

Resend

Architecture:

```text
                    Internet

        ┌───────────────┴──────────────┐
        │                              │
      Vercel                         Render
     Next.js                    ┌──────┴──────┐
                                │             │
                              API          Worker
                                │             │
                                └──────┬──────┘
                                       │
                          ┌────────────┼─────────────┐
                          │            │             │
                        Neon       Temporal        R2
                     PostgreSQL      Cloud        Storage
```

---

## 30. Repository Structure

```text
jobpulse/
│
├── apps/
│   ├── api/
│   │   └── src/jobpulse/
│   │       ├── api/
│   │       ├── core/
│   │       ├── domain/
│   │       ├── db/
│   │       ├── repositories/
│   │       └── services/
│   │
│   ├── worker/
│   │   └── src/jobpulse_worker/
│   │       ├── workflows/
│   │       ├── activities/
│   │       └── runtime/
│   │
│   └── web/
│       ├── app/
│       ├── components/
│       ├── features/
│       └── lib/
│
├── packages/
│   └── python/
│       ├── domain/
│       ├── sources/
│       ├── eligibility/
│       ├── ranking/
│       ├── intelligence/
│       └── notifications/
│
├── migrations/
│
├── docs/
│   ├── architecture/
│   ├── adr/
│   ├── diagrams/
│   ├── failure-modes.md
│   ├── security.md
│   └── tradeoffs.md
│
├── tests/
│   ├── fixtures/
│   ├── unit/
│   ├── integration/
│   └── e2e/
│
├── infra/
│   ├── docker/
│   └── compose/
│
├── .github/
│   └── workflows/
│
├── docker-compose.yml
├── Makefile
├── pyproject.toml
├── README.md
└── LICENSE
```

---

## 31. Technologies Intentionally Excluded

| Technology | Decision | Reason |
|---|---|---|
| Kafka | No | Current scale does not require it |
| Kubernetes | No | Unnecessary operational overhead |
| Elasticsearch | No | PostgreSQL FTS is sufficient |
| Pinecone | No | pgvector is sufficient |
| MongoDB | No | Domain is relational |
| Redis | Not initially | Temporal handles workflow durability |
| Celery | No | Temporal replaces it |
| Airflow | No | Wrong orchestration model |
| LangChain | No | Low value for this architecture |
| LangGraph | Not core | No genuine multi-agent graph required |
| CrewAI | No | Unnecessary abstraction |
| GraphQL | No | REST is sufficient |
| Microservices | No | Modular monolith + worker is cleaner |

---

## 32. Final Locked Stack

```text
LANGUAGES
Python 3.14
TypeScript

BACKEND
FastAPI
Pydantic
SQLAlchemy
Alembic
psycopg
HTTPX
structlog

WORKFLOWS
Temporal

DATABASE
PostgreSQL 18
pgvector
pg_trgm
PostgreSQL FTS

AI
OpenAI Responses API
Structured Outputs
Embeddings

INGESTION
HTTPX
selectolax
feedparser
Playwright fallback
nh3

STORAGE
Cloudflare R2

FRONTEND
Next.js 16
React 19
TypeScript
Tailwind CSS 4
shadcn/ui
TanStack Query
TanStack Table
Zustand
Zod
React Hook Form
Recharts

AUTH
Auth.js
GitHub OAuth

NOTIFICATIONS
Resend
Webhooks

OBSERVABILITY
OpenTelemetry
structlog
Prometheus-compatible metrics
Sentry

TESTING
pytest
pytest-asyncio
Hypothesis
respx
Testcontainers
Vitest
React Testing Library
Playwright

SECURITY
Gitleaks
Semgrep
CodeQL
Trivy
pip-audit
npm audit

CI/CD
GitHub Actions

LOCAL DEVELOPMENT
Docker
Docker Compose
uv
pnpm

PRODUCTION
Vercel
Render
Neon PostgreSQL
Temporal Cloud
Cloudflare R2
Resend
```

---

## 33. Build Order

Recommended implementation order for Antigravity:

```text
Phase 1
Domain models
PostgreSQL schema
FastAPI skeleton

Phase 2
Source adapter contract
Greenhouse + Lever + Ashby adapters
Normalization + deduplication

Phase 3
Temporal workflows
Adaptive polling
Retries and checkpoints

Phase 4
Eligibility engine
Profile configuration
Decision persistence

Phase 5
OpenAI structured extraction
Embeddings
Ranking

Phase 6
Notifications
Resend + webhook

Phase 7
Next.js dashboard
Job detail explainability

Phase 8
Observability
Security hardening
CI/CD

Phase 9
Deployment
Smoke tests
Demo data
Submission documentation
```

---

## 34. Engineering Standard

Before marking any feature complete:

- strict typing passes
- linting passes
- unit tests pass
- integration tests pass
- security scans pass
- migrations are reversible
- logs contain useful context
- failures are observable
- retries are bounded
- external calls have timeouts
- decisions are auditable
- no secrets are committed
- README remains accurate
- Docker Compose starts cleanly from scratch

This document is the architecture baseline for JobPulse. Any major deviation should be recorded in an Architecture Decision Record under `docs/adr/`.
