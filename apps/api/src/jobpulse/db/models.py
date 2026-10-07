"""SQLAlchemy 2.x ORM models (PostgreSQL 18 + pgvector + pg_trgm).

Primary keys are native UUIDv7 (``uuidv7()``, PostgreSQL 18) so they are time-ordered
and index-friendly. Every table that records a decision is append-only; the latest
row is selected by ``created_at``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from jobpulse.core.config import EMBEDDING_DIMENSIONS

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

UUID_PK = text("uuidv7()")
NOW = text("now()")
# Tenant rows default to the transaction's workspace scope (see jobpulse.db.tenancy), so a
# write made in a workspace scope is stamped automatically and RLS WITH CHECK rejects others.
CURRENT_WORKSPACE = text("NULLIF(current_setting('app.workspace_id', true), '')::uuid")
# Rows created before multi-tenancy live here (migration 0002); also the single-tenant default.
DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")
PLANS = ("free", "pro", "team")
WORKSPACE_ROLES = ("owner", "admin", "member")
IDENTITY_PROVIDERS = ("github", "google", "microsoft", "email")


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[dict[str, Any]]: JSONB,
        uuid.UUID: UUID(as_uuid=True),
        datetime: DateTime(timezone=True),
    }


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=UUID_PK)


def _created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=NOW)


def _workspace() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, server_default=CURRENT_WORKSPACE)


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class Workspace(Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(63), unique=True)
    plan: Mapped[str] = mapped_column(String(20), server_default="free")
    personal: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        CheckConstraint(f"plan IN ({_in(PLANS)})", name="plan_valid"),
        CheckConstraint("slug ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'", name="slug_format"),
    )


class Membership(Base):
    __tablename__ = "memberships"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True, server_default=CURRENT_WORKSPACE
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True)
    role: Mapped[str] = mapped_column(String(20), server_default="member")
    created_at: Mapped[datetime] = _created()

    __table_args__ = (CheckConstraint(f"role IN ({_in(WORKSPACE_ROLES)})", name="role_valid"),)


class User(Base):
    """A person. Sign-in identities (GitHub, Google, ...) link to it; roles live in memberships."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    display_name: Mapped[str] = mapped_column(String(200), server_default="")
    email: Mapped[str | None] = mapped_column(String(320))
    # Legacy single-tenant columns (pre-0003); unused, dropped by the next contract migration.
    github_login: Mapped[str | None] = mapped_column(String(100), unique=True, deferred=True)
    role: Mapped[str] = mapped_column(String(20), server_default="PUBLIC_DEMO", deferred=True)
    created_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint("role IN ('PUBLIC_DEMO','OWNER')", name="role_valid"),)


class Identity(Base):
    """An external sign-in identity, e.g. provider='github', subject='583231' (immutable id)."""

    __tablename__ = "identities"

    provider: Mapped[str] = mapped_column(String(20), primary_key=True)
    subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (CheckConstraint(f"provider IN ({_in(IDENTITY_PROVIDERS)})", name="provider_valid"),)


class Invitation(Base):
    """Single-use, expiring invitation to a workspace. Only the token's SHA-256 is stored."""

    __tablename__ = "invitations"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace()
    email: Mapped[str | None] = mapped_column(String(320))
    role: Mapped[str] = mapped_column(String(20), server_default="member")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    accepted_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (CheckConstraint(f"role IN ({_in(WORKSPACE_ROLES)})", name="role_valid"),)


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace()
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), unique=True)
    display_name: Mapped[str] = mapped_column(String(200))
    target_roles: Mapped[list[str]] = mapped_column(ARRAY(String(200)), server_default="{}")
    skills: Mapped[list[str]] = mapped_column(ARRAY(String(100)), server_default="{}")
    years_experience: Mapped[Decimal] = mapped_column(Numeric(4, 1), server_default="0")
    seniority: Mapped[str] = mapped_column(String(20), server_default="mid")
    home_country: Mapped[str] = mapped_column(String(100), server_default="India")
    timezone: Mapped[str] = mapped_column(String(20), server_default="IST")
    summary: Mapped[str] = mapped_column(Text, server_default="")
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    notification_email: Mapped[str | None] = mapped_column(String(320))
    webhook_url: Mapped[str | None] = mapped_column(String(2048))
    notify_min_score: Mapped[float] = mapped_column(Float, server_default="0.7")
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false")
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str | None] = mapped_column(String(100))
    embedding_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW, onupdate=NOW)

    __table_args__ = (
        CheckConstraint("notify_min_score >= 0 AND notify_min_score <= 1", name="notify_min_score_range"),
    )


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str] = mapped_column(String(253), unique=True)
    created_at: Mapped[datetime] = _created()


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[uuid.UUID] = _pk()
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(30))
    # What the board points at (ATS token or normalized URL): one shared poller per board.
    locator: Mapped[str] = mapped_column(String(2048))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, server_default="true")
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, server_default="900")
    min_poll_interval_seconds: Mapped[int] = mapped_column(Integer, server_default="300")
    max_poll_interval_seconds: Mapped[int] = mapped_column(Integer, server_default="3600")
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default="0")
    circuit_open_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_new_job_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW, onupdate=NOW)

    company: Mapped[Company] = relationship(lazy="joined")

    __table_args__ = (
        UniqueConstraint("kind", "locator", name="uq_sources_kind_locator"),
        CheckConstraint(
            "min_poll_interval_seconds <= poll_interval_seconds AND poll_interval_seconds <= max_poll_interval_seconds",
            name="poll_interval_bounds",
        ),
        CheckConstraint("min_poll_interval_seconds >= 60", name="min_poll_floor"),
    )


class SourceSubscription(Base):
    """A workspace follows a (global, shared) source; its jobs are evaluated for that workspace's profiles."""

    __tablename__ = "source_subscriptions"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True, server_default=CURRENT_WORKSPACE
    )
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    # Paused for this workspace only: no evaluations here; the board polls while anyone is active.
    paused: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = _created()


class SourceCheckpoint(Base):
    __tablename__ = "source_checkpoints"

    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW, onupdate=NOW)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="RESTRICT"))
    external_id: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(500))
    normalized_location: Mapped[str | None] = mapped_column(String(500))
    department: Mapped[str | None] = mapped_column(String(200))
    employment_type: Mapped[str | None] = mapped_column(String(100))
    remote_policy: Mapped[str] = mapped_column(String(20), server_default="unknown")
    seniority: Mapped[str] = mapped_column(String(20), server_default="unknown")
    description_text: Mapped[str] = mapped_column(Text, server_default="")
    description_html: Mapped[str] = mapped_column(Text, server_default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = _created()
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    duplicate_of_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, server_default="1")
    # Catalogue lifecycle only (discovered / duplicate / closed). Per-profile evaluation state
    # lives in ProfileJob.
    workflow_state: Mapped[str] = mapped_column(String(30), server_default="discovered")
    # DEPRECATED since migration 0002 (expand/contract): never read or written - deferred so
    # they are not even loaded. Dropped, with their indexes, by the next contract migration.
    legacy_eligibility_status: Mapped[str] = mapped_column(
        "eligibility_status", String(20), server_default="pending", deferred=True
    )
    legacy_match_score: Mapped[float | None] = mapped_column("match_score", Float, deferred=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_hash: Mapped[str | None] = mapped_column(String(64))
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(description_text, '')), 'B')",
            persisted=True,
        ),
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW, onupdate=NOW)

    source: Mapped[Source] = relationship(lazy="raise")
    company: Mapped[Company] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
        Index("ix_jobs_company_id", "company_id"),
        Index("ix_jobs_source_id", "source_id"),
        Index("ix_jobs_published_at", "published_at"),
        Index("ix_jobs_normalized_location", "normalized_location"),
        Index("ix_jobs_remote_policy", "remote_policy"),
        Index("ix_jobs_seniority", "seniority"),
        Index("ix_jobs_eligibility_status", "eligibility_status"),
        Index("ix_jobs_workflow_state", "workflow_state"),
        Index("ix_jobs_fingerprint", "fingerprint"),
        Index("ix_jobs_canonical_url", "canonical_url"),
        Index("ix_jobs_match_score", "match_score"),
        Index("ix_jobs_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_jobs_normalized_title_trgm",
            "normalized_title",
            postgresql_using="gin",
            postgresql_ops={"normalized_title": "gin_trgm_ops"},
        ),
        Index(
            "ix_jobs_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        CheckConstraint("eligibility_status IN ('pending','eligible','ineligible')", name="eligibility_status_valid"),
    )


class ProfileJob(Base):
    """One profile's view of one job: eligibility, score and evaluation progress."""

    __tablename__ = "profile_jobs"

    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), primary_key=True, index=True)
    workspace_id: Mapped[uuid.UUID] = _workspace()
    eligibility_status: Mapped[str] = mapped_column(String(20), server_default="pending")
    state: Mapped[str] = mapped_column(String(30), server_default="discovered")
    match_score: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW, onupdate=NOW)

    __table_args__ = (
        Index("ix_profile_jobs_profile_status_score", "profile_id", "eligibility_status", "match_score"),
        CheckConstraint("eligibility_status IN ('pending','eligible','ineligible')", name="eligibility_status_valid"),
        CheckConstraint("match_score IS NULL OR (match_score >= 0 AND match_score <= 1)", name="match_score_range"),
    )


class JobVersion(Base):
    __tablename__ = "job_versions"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(500))
    description_text: Mapped[str] = mapped_column(Text)
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("raw_snapshots.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (UniqueConstraint("job_id", "version", name="uq_job_versions_job_version"),)


class RawSnapshot(Base):
    __tablename__ = "raw_snapshots"

    id: Mapped[uuid.UUID] = _pk()
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    snapshot_key: Mapped[str] = mapped_column(String(1024), unique=True)
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    content_type: Mapped[str] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    fetched_at: Mapped[datetime] = _created()


class EligibilityDecision(Base):
    __tablename__ = "eligibility_decisions"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = _workspace()
    stage: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20))
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    unresolved: Mapped[list[str]] = mapped_column(ARRAY(String(50)), server_default="{}")
    policy_hash: Mapped[str] = mapped_column(String(64))
    content_hash: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        Index("ix_eligibility_decisions_job_created", "job_id", "created_at"),
        Index("ix_eligibility_decisions_status", "status"),
        CheckConstraint("stage IN ('deterministic','post_enrichment')", name="stage_valid"),
    )


class JobIntelligenceRecord(Base):
    __tablename__ = "job_intelligence"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    content_hash: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    confidence: Mapped[float] = mapped_column(Float)
    escalated: Mapped[bool] = mapped_column(Boolean, server_default="false")
    input_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), server_default="0")
    created_at: Mapped[datetime] = _created()

    __table_args__ = (UniqueConstraint("job_id", "content_hash", name="uq_job_intelligence_job_hash"),)


class MatchScore(Base):
    __tablename__ = "match_scores"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = _workspace()
    final_score: Mapped[float] = mapped_column(Float)
    actionable: Mapped[bool] = mapped_column(Boolean)
    components: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB)
    matched_skills: Mapped[list[str]] = mapped_column(ARRAY(String(100)), server_default="{}")
    missing_skills: Mapped[list[str]] = mapped_column(ARRAY(String(100)), server_default="{}")
    content_hash: Mapped[str] = mapped_column(String(64))
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        Index("ix_match_scores_job_created", "job_id", "created_at"),
        CheckConstraint("final_score >= 0 AND final_score <= 1", name="final_score_range"),
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = _workspace()
    channel: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), server_default="pending")
    dedupe_key: Mapped[str] = mapped_column(String(255), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()

    __table_args__ = (CheckConstraint("status IN ('pending','sent','failed','skipped')", name="status_valid"),)


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[uuid.UUID] = _pk()
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    profile_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID] = _workspace()
    status: Mapped[str] = mapped_column(String(30), server_default="interested")
    notes: Mapped[str] = mapped_column(Text, server_default="")
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=NOW, onupdate=NOW)

    job: Mapped[Job] = relationship(lazy="raise")

    __table_args__ = (
        UniqueConstraint("job_id", "profile_id", name="uq_applications_job_profile"),
        CheckConstraint(
            "status IN ('interested','applied','interviewing','offer','rejected','withdrawn')",
            name="status_valid",
        ),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[uuid.UUID] = _pk()
    workflow_id: Mapped[str] = mapped_column(String(255))
    run_id: Mapped[str | None] = mapped_column(String(255))
    workflow_type: Mapped[str] = mapped_column(String(100))
    source_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sources.id", ondelete="SET NULL"), index=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    # NULL for catalogue runs (polling/discovery); set for per-profile evaluation runs.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, server_default=CURRENT_WORKSPACE
    )
    status: Mapped[str] = mapped_column(String(20), server_default="running")
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _created()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("workflow_id", "run_id", name="uq_workflow_runs_workflow_run"),
        Index("ix_workflow_runs_started_at", "started_at"),
        CheckConstraint("status IN ('running','completed','failed','cancelled')", name="status_valid"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace()
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(100))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
        Index("ix_audit_events_created_at", "created_at"),
    )
