# ADR 0004 — Deviations from the original tech-stack document

**Status:** Accepted

| Topic | Spec | Implemented | Reason |
|---|---|---|---|
| `JobSource.discover` return type | `list[RawJob]` | `DiscoveryResult` (jobs + updated checkpoint + raw payload + skipped items) | Conditional GETs (ETag/Last-Modified) and raw snapshots need more than the job list. |
| Domain package layout | several `packages/python/*` packages | one `jobpulse_core` package with sub-packages | Single installable unit; same module boundaries, simpler dependency graph. |
| DB models location | `apps/api/src/jobpulse/db` | same; the worker depends on the API package | One definition of repositories/services shared by API and worker. |
| TypeScript | latest | 5.9 | TypeScript 7 (native) is not yet a drop-in for Next.js type-checking. |
| Rate limiting | unspecified | in-process token bucket | Adequate for one API instance; move to the edge when scaling horizontally (see tradeoffs). |
