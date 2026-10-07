# ADR 0003 — PostgreSQL as the only datastore

**Status:** Accepted

## Decision
PostgreSQL 18 holds all transactional, search and vector data: pgvector (HNSW) for semantic
similarity, `pg_trgm` for fuzzy titles, a generated `tsvector` with GIN for full-text search, and
native `uuidv7()` keys. Raw source payloads live in object storage (Cloudflare R2), referenced by
key and SHA-256 hash.

## Rejected alternatives
Elasticsearch (FTS is sufficient), Pinecone/Qdrant/Weaviate (pgvector is sufficient at this
scale), MongoDB (the domain is relational and needs constraints and joins).

## Consequences
One backup/restore story, transactional consistency between jobs and their decisions, and the
embedding dimension (1536) is fixed by migration — config validation enforces it at startup.
