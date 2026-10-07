"""initial schema: all core entities, pgvector + pg_trgm + FTS indexes.

Revision ID: 0001
Revises:
Create Date: 2026-10-06 14:28:08.243836+00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy.vector
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # uuidv7() is native in PostgreSQL 18; vector + pg_trgm are required extensions.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table(
        "audit_events",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"], unique=False)
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"], unique=False)
    op.create_table(
        "companies",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("domain", name=op.f("uq_companies_domain")),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("github_login", sa.String(length=100), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=20), server_default="PUBLIC_DEMO", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('PUBLIC_DEMO','OWNER')", name=op.f("ck_users_role_valid")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("github_login", name=op.f("uq_users_github_login")),
    )
    op.create_table(
        "profiles",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("target_roles", postgresql.ARRAY(sa.String(length=200)), server_default="{}", nullable=False),
        sa.Column("skills", postgresql.ARRAY(sa.String(length=100)), server_default="{}", nullable=False),
        sa.Column("years_experience", sa.Numeric(precision=4, scale=1), server_default="0", nullable=False),
        sa.Column("seniority", sa.String(length=20), server_default="mid", nullable=False),
        sa.Column("home_country", sa.String(length=100), server_default="India", nullable=False),
        sa.Column("timezone", sa.String(length=20), server_default="IST", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("notification_email", sa.String(length=320), nullable=True),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column("notify_min_score", sa.Float(), server_default="0.7", nullable=False),
        sa.Column("notifications_enabled", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=True),
        sa.Column("embedding_model", sa.String(length=100), nullable=True),
        sa.Column("embedding_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "notify_min_score >= 0 AND notify_min_score <= 1", name=op.f("ck_profiles_notify_min_score_range")
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_profiles_user_id_users"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profiles")),
        sa.UniqueConstraint("user_id", name=op.f("uq_profiles_user_id")),
    )
    op.create_table(
        "sources",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("poll_interval_seconds", sa.Integer(), server_default="900", nullable=False),
        sa.Column("min_poll_interval_seconds", sa.Integer(), server_default="300", nullable=False),
        sa.Column("max_poll_interval_seconds", sa.Integer(), server_default="3600", nullable=False),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("circuit_open_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_new_job_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "min_poll_interval_seconds <= poll_interval_seconds AND poll_interval_seconds <= max_poll_interval_seconds",
            name=op.f("ck_sources_poll_interval_bounds"),
        ),
        sa.CheckConstraint("min_poll_interval_seconds >= 60", name=op.f("ck_sources_min_poll_floor")),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_sources_company_id_companies"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint("kind", "name", name="uq_sources_kind_name"),
    )
    op.create_index(op.f("ix_sources_company_id"), "sources", ["company_id"], unique=False)
    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("company_id", sa.UUID(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("canonical_url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("normalized_title", sa.String(length=500), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("normalized_location", sa.String(length=500), nullable=True),
        sa.Column("department", sa.String(length=200), nullable=True),
        sa.Column("employment_type", sa.String(length=100), nullable=True),
        sa.Column("remote_policy", sa.String(length=20), server_default="unknown", nullable=False),
        sa.Column("seniority", sa.String(length=20), server_default="unknown", nullable=False),
        sa.Column("description_text", sa.Text(), server_default="", nullable=False),
        sa.Column("description_html", sa.Text(), server_default="", nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("duplicate_of_id", sa.UUID(), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("eligibility_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("workflow_state", sa.String(length=30), server_default="discovered", nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.vector.VECTOR(dim=1536), nullable=True),
        sa.Column("embedding_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('english', coalesce(title, '')), 'A') || setweight(to_tsvector('english', coalesce(description_text, '')), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "eligibility_status IN ('pending','eligible','ineligible')", name=op.f("ck_jobs_eligibility_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], name=op.f("fk_jobs_company_id_companies"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["duplicate_of_id"], ["jobs.id"], name=op.f("fk_jobs_duplicate_of_id_jobs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_jobs_source_id_sources"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("source_id", "external_id", name="uq_jobs_source_external"),
    )
    op.create_index("ix_jobs_canonical_url", "jobs", ["canonical_url"], unique=False)
    op.create_index("ix_jobs_company_id", "jobs", ["company_id"], unique=False)
    op.create_index("ix_jobs_eligibility_status", "jobs", ["eligibility_status"], unique=False)
    op.create_index(
        "ix_jobs_embedding_hnsw",
        "jobs",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_jobs_fingerprint", "jobs", ["fingerprint"], unique=False)
    op.create_index("ix_jobs_match_score", "jobs", ["match_score"], unique=False)
    op.create_index("ix_jobs_normalized_location", "jobs", ["normalized_location"], unique=False)
    op.create_index(
        "ix_jobs_normalized_title_trgm",
        "jobs",
        ["normalized_title"],
        unique=False,
        postgresql_using="gin",
        postgresql_ops={"normalized_title": "gin_trgm_ops"},
    )
    op.create_index("ix_jobs_published_at", "jobs", ["published_at"], unique=False)
    op.create_index("ix_jobs_remote_policy", "jobs", ["remote_policy"], unique=False)
    op.create_index("ix_jobs_search_vector", "jobs", ["search_vector"], unique=False, postgresql_using="gin")
    op.create_index("ix_jobs_seniority", "jobs", ["seniority"], unique=False)
    op.create_index("ix_jobs_source_id", "jobs", ["source_id"], unique=False)
    op.create_index("ix_jobs_workflow_state", "jobs", ["workflow_state"], unique=False)
    op.create_table(
        "source_checkpoints",
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("state", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_source_checkpoints_source_id_sources"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("source_id", name=op.f("pk_source_checkpoints")),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="interested", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('interested','applied','interviewing','offer','rejected','withdrawn')",
            name=op.f("ck_applications_status_valid"),
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_applications_job_id_jobs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], name=op.f("fk_applications_profile_id_profiles"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_applications")),
        sa.UniqueConstraint("job_id", "profile_id", name="uq_applications_job_profile"),
    )
    op.create_table(
        "eligibility_decisions",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("unresolved", postgresql.ARRAY(sa.String(length=50)), server_default="{}", nullable=False),
        sa.Column("policy_hash", sa.String(length=64), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "stage IN ('deterministic','post_enrichment')", name=op.f("ck_eligibility_decisions_stage_valid")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_eligibility_decisions_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["profiles.id"],
            name=op.f("fk_eligibility_decisions_profile_id_profiles"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eligibility_decisions")),
    )
    op.create_index(
        "ix_eligibility_decisions_job_created", "eligibility_decisions", ["job_id", "created_at"], unique=False
    )
    op.create_index("ix_eligibility_decisions_status", "eligibility_decisions", ["status"], unique=False)
    op.create_table(
        "job_intelligence",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("escalated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_job_intelligence_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_intelligence")),
        sa.UniqueConstraint("job_id", "content_hash", name="uq_job_intelligence_job_hash"),
    )
    op.create_table(
        "match_scores",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("final_score", sa.Float(), nullable=False),
        sa.Column("actionable", sa.Boolean(), nullable=False),
        sa.Column("components", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("weights", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("matched_skills", postgresql.ARRAY(sa.String(length=100)), server_default="{}", nullable=False),
        sa.Column("missing_skills", postgresql.ARRAY(sa.String(length=100)), server_default="{}", nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("final_score >= 0 AND final_score <= 1", name=op.f("ck_match_scores_final_score_range")),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_match_scores_job_id_jobs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], name=op.f("fk_match_scores_profile_id_profiles"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_scores")),
    )
    op.create_index("ix_match_scores_job_created", "match_scores", ["job_id", "created_at"], unique=False)
    op.create_table(
        "notifications",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','sent','failed','skipped')", name=op.f("ck_notifications_status_valid")
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_notifications_job_id_jobs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], name=op.f("fk_notifications_profile_id_profiles"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notifications_dedupe_key")),
    )
    op.create_index(op.f("ix_notifications_job_id"), "notifications", ["job_id"], unique=False)
    op.create_table(
        "raw_snapshots",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("snapshot_key", sa.String(length=1024), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_raw_snapshots_job_id_jobs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_raw_snapshots_source_id_sources"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_snapshots")),
        sa.UniqueConstraint("snapshot_key", name=op.f("uq_raw_snapshots_snapshot_key")),
    )
    op.create_index(op.f("ix_raw_snapshots_job_id"), "raw_snapshots", ["job_id"], unique=False)
    op.create_index(op.f("ix_raw_snapshots_source_id"), "raw_snapshots", ["source_id"], unique=False)
    op.create_table(
        "workflow_runs",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workflow_id", sa.String(length=255), nullable=False),
        sa.Column("run_id", sa.String(length=255), nullable=True),
        sa.Column("workflow_type", sa.String(length=100), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="running", nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running','completed','failed','cancelled')", name=op.f("ck_workflow_runs_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_workflow_runs_job_id_jobs"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_workflow_runs_source_id_sources"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workflow_runs")),
        sa.UniqueConstraint("workflow_id", "run_id", name="uq_workflow_runs_workflow_run"),
    )
    op.create_index(op.f("ix_workflow_runs_job_id"), "workflow_runs", ["job_id"], unique=False)
    op.create_index(op.f("ix_workflow_runs_source_id"), "workflow_runs", ["source_id"], unique=False)
    op.create_index("ix_workflow_runs_started_at", "workflow_runs", ["started_at"], unique=False)
    op.create_table(
        "job_versions",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("description_text", sa.Text(), nullable=False),
        sa.Column("snapshot_id", sa.UUID(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_job_versions_job_id_jobs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["raw_snapshots.id"],
            name=op.f("fk_job_versions_snapshot_id_raw_snapshots"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_versions")),
        sa.UniqueConstraint("job_id", "version", name="uq_job_versions_job_version"),
    )


def downgrade() -> None:
    op.drop_table("job_versions")
    op.drop_index("ix_workflow_runs_started_at", table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_source_id"), table_name="workflow_runs")
    op.drop_index(op.f("ix_workflow_runs_job_id"), table_name="workflow_runs")
    op.drop_table("workflow_runs")
    op.drop_index(op.f("ix_raw_snapshots_source_id"), table_name="raw_snapshots")
    op.drop_index(op.f("ix_raw_snapshots_job_id"), table_name="raw_snapshots")
    op.drop_table("raw_snapshots")
    op.drop_index(op.f("ix_notifications_job_id"), table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_match_scores_job_created", table_name="match_scores")
    op.drop_table("match_scores")
    op.drop_table("job_intelligence")
    op.drop_index("ix_eligibility_decisions_status", table_name="eligibility_decisions")
    op.drop_index("ix_eligibility_decisions_job_created", table_name="eligibility_decisions")
    op.drop_table("eligibility_decisions")
    op.drop_table("applications")
    op.drop_table("source_checkpoints")
    op.drop_index("ix_jobs_workflow_state", table_name="jobs")
    op.drop_index("ix_jobs_source_id", table_name="jobs")
    op.drop_index("ix_jobs_seniority", table_name="jobs")
    op.drop_index("ix_jobs_search_vector", table_name="jobs", postgresql_using="gin")
    op.drop_index("ix_jobs_remote_policy", table_name="jobs")
    op.drop_index("ix_jobs_published_at", table_name="jobs")
    op.drop_index(
        "ix_jobs_normalized_title_trgm",
        table_name="jobs",
        postgresql_using="gin",
        postgresql_ops={"normalized_title": "gin_trgm_ops"},
    )
    op.drop_index("ix_jobs_normalized_location", table_name="jobs")
    op.drop_index("ix_jobs_match_score", table_name="jobs")
    op.drop_index("ix_jobs_fingerprint", table_name="jobs")
    op.drop_index(
        "ix_jobs_embedding_hnsw",
        table_name="jobs",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_index("ix_jobs_eligibility_status", table_name="jobs")
    op.drop_index("ix_jobs_company_id", table_name="jobs")
    op.drop_index("ix_jobs_canonical_url", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index(op.f("ix_sources_company_id"), table_name="sources")
    op.drop_table("sources")
    op.drop_table("profiles")
    op.drop_table("users")
    op.drop_table("companies")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")
    op.execute("DROP EXTENSION IF EXISTS pg_trgm")
    op.execute("DROP EXTENSION IF EXISTS vector")
