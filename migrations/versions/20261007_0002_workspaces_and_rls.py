"""multi-tenancy: workspaces, memberships, subscriptions, per-profile job state, RLS.

Expand-only: legacy jobs.eligibility_status / jobs.match_score stay (unused) until the next
contract migration, so the previous release keeps working during a rolling deploy.

Every pre-existing row is attached to the Default workspace. Row-level security is enabled
and FORCED on every tenant table (table owners are subject too); the application switches
to the non-owner role ``jobpulse_app`` per transaction because superusers bypass RLS.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07 12:00:00+00:00
"""

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "jobpulse_app"
DEFAULT_WORKSPACE = uuid.UUID("00000000-0000-7000-8000-000000000001")
CURRENT_WORKSPACE = sa.text("NULLIF(current_setting('app.workspace_id', true), '')::uuid")
SYSTEM = "current_setting('app.system_scope', true) = 'on'"
IN_WORKSPACE = "{column} = NULLIF(current_setting('app.workspace_id', true), '')::uuid"

# table -> nullable workspace_id (catalogue rows visible to everyone)
WORKSPACE_COLUMNS = {
    "profiles": False,
    "eligibility_decisions": False,
    "match_scores": False,
    "notifications": False,
    "applications": False,
    "audit_events": False,
    "workflow_runs": True,
}
RLS_TABLES = {
    # table: column holding the workspace id
    "workspaces": "id",
    "memberships": "workspace_id",
    "source_subscriptions": "workspace_id",
    "profile_jobs": "workspace_id",
    **{table: "workspace_id" for table in WORKSPACE_COLUMNS},
}
PER_PROFILE_STATES = ("eligibility_checked", "enriched", "ranked", "notified", "rejected")


def _policy_expression(table: str, column: str) -> str:
    clauses = [SYSTEM, IN_WORKSPACE.format(column=column)]
    if WORKSPACE_COLUMNS.get(table):
        clauses.append(f"{column} IS NULL")
    return " OR ".join(f"({clause})" for clause in clauses)


def _create_app_role() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$
        """
    )
    # The login role must be able to SET ROLE to the app role (no-op for superusers).
    op.execute(f"GRANT {APP_ROLE} TO CURRENT_USER")
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}")


def _create_tables() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=63), nullable=False),
        sa.Column("plan", sa.String(length=20), server_default="free", nullable=False),
        sa.Column("personal", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("plan IN ('free', 'pro', 'team')", name=op.f("ck_workspaces_plan_valid")),
        sa.CheckConstraint("slug ~ '^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$'", name=op.f("ck_workspaces_slug_format")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.UniqueConstraint("slug", name=op.f("uq_workspaces_slug")),
    )
    op.create_table(
        "memberships",
        sa.Column("workspace_id", sa.UUID(), server_default=CURRENT_WORKSPACE, nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", sa.String(length=20), server_default="member", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name=op.f("ck_memberships_role_valid")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_memberships_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_memberships")),
    )
    op.create_index(op.f("ix_memberships_user_id"), "memberships", ["user_id"], unique=False)
    op.create_table(
        "source_subscriptions",
        sa.Column("workspace_id", sa.UUID(), server_default=CURRENT_WORKSPACE, nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_source_subscriptions_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["sources.id"], name=op.f("fk_source_subscriptions_source_id_sources"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("workspace_id", "source_id", name=op.f("pk_source_subscriptions")),
    )
    op.create_index(op.f("ix_source_subscriptions_source_id"), "source_subscriptions", ["source_id"], unique=False)
    op.create_table(
        "profile_jobs",
        sa.Column("profile_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), server_default=CURRENT_WORKSPACE, nullable=False),
        sa.Column("eligibility_status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("state", sa.String(length=30), server_default="discovered", nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "eligibility_status IN ('pending','eligible','ineligible')",
            name=op.f("ck_profile_jobs_eligibility_status_valid"),
        ),
        sa.CheckConstraint(
            "match_score IS NULL OR (match_score >= 0 AND match_score <= 1)",
            name=op.f("ck_profile_jobs_match_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], name=op.f("fk_profile_jobs_profile_id_profiles"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_profile_jobs_job_id_jobs"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_profile_jobs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("profile_id", "job_id", name=op.f("pk_profile_jobs")),
    )
    op.create_index(op.f("ix_profile_jobs_job_id"), "profile_jobs", ["job_id"], unique=False)
    op.create_index(op.f("ix_profile_jobs_workspace_id"), "profile_jobs", ["workspace_id"], unique=False)
    op.create_index(
        "ix_profile_jobs_profile_status_score",
        "profile_jobs",
        ["profile_id", "eligibility_status", "match_score"],
        unique=False,
    )


def _add_workspace_columns() -> None:
    for table, nullable in WORKSPACE_COLUMNS.items():
        op.add_column(table, sa.Column("workspace_id", sa.UUID(), server_default=CURRENT_WORKSPACE, nullable=True))
        target = sa.table(table, sa.column("workspace_id", sa.UUID()), sa.column("job_id", sa.UUID()))
        statement = sa.update(target).values(workspace_id=DEFAULT_WORKSPACE)
        if table == "workflow_runs":
            # Evaluation runs belong to the (only) tenant; polling/discovery runs are catalogue.
            statement = statement.where(target.c.job_id.is_not(None))
        op.execute(statement)
        if not nullable:
            op.alter_column(table, "workspace_id", nullable=False)
        op.create_foreign_key(
            op.f(f"fk_{table}_workspace_id_workspaces"),
            table,
            "workspaces",
            ["workspace_id"],
            ["id"],
            ondelete="CASCADE",
        )
        op.create_index(op.f(f"ix_{table}_workspace_id"), table, ["workspace_id"], unique=False)


def _backfill() -> None:
    ws = {"ws": DEFAULT_WORKSPACE}
    states = sa.bindparam("states", list(PER_PROFILE_STATES), expanding=True)
    op.execute(
        sa.text("INSERT INTO workspaces (id, name, slug, plan) VALUES (:ws, 'Default', 'default', 'team')").bindparams(
            **ws
        )
    )
    _add_workspace_columns()
    op.execute(
        sa.text(
            "INSERT INTO memberships (workspace_id, user_id, role) SELECT :ws, id, 'owner' FROM users WHERE role = 'OWNER'"
        ).bindparams(**ws)
    )
    op.execute(
        sa.text("INSERT INTO source_subscriptions (workspace_id, source_id) SELECT :ws, id FROM sources").bindparams(
            **ws
        )
    )
    # Per-job evaluation state moves to the (single, primary) profile.
    op.execute(
        sa.text(
            """
            INSERT INTO profile_jobs (profile_id, job_id, workspace_id, eligibility_status, state, match_score)
            SELECT p.id, j.id, :ws, j.eligibility_status,
                   CASE WHEN j.workflow_state IN :states THEN j.workflow_state ELSE 'discovered' END,
                   j.match_score
            FROM jobs j
            CROSS JOIN (SELECT id FROM profiles ORDER BY created_at ASC LIMIT 1) p
            WHERE j.duplicate_of_id IS NULL
            """
        ).bindparams(states, **ws)
    )
    op.execute(
        sa.text("UPDATE jobs SET workflow_state = 'discovered' WHERE workflow_state IN :states").bindparams(states)
    )
    # Notification idempotency keys now include the profile (one alert per profile per job).
    op.execute(
        "UPDATE notifications SET dedupe_key = dedupe_key || ':' || profile_id::text WHERE dedupe_key NOT LIKE '%:%:%:%'"
    )


def _enable_rls() -> None:
    for table, column in RLS_TABLES.items():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        expression = _policy_expression(table, column)
        op.execute(f"CREATE POLICY tenant_isolation ON {table} USING ({expression}) WITH CHECK ({expression})")


def upgrade() -> None:
    _create_tables()
    _backfill()
    _create_app_role()  # after the tables exist so ALL TABLES grants cover them
    _enable_rls()


def downgrade() -> None:
    for table in RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS tenant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
    op.execute(
        """
        UPDATE jobs j SET eligibility_status = pj.eligibility_status, match_score = pj.match_score,
                          workflow_state = CASE WHEN pj.state = 'discovered' THEN j.workflow_state ELSE pj.state END
        FROM profile_jobs pj
        WHERE pj.job_id = j.id
          AND pj.profile_id = (SELECT id FROM profiles ORDER BY created_at ASC LIMIT 1)
        """
    )
    for table in WORKSPACE_COLUMNS:
        op.drop_index(op.f(f"ix_{table}_workspace_id"), table_name=table)
        op.drop_constraint(op.f(f"fk_{table}_workspace_id_workspaces"), table, type_="foreignkey")
        op.drop_column(table, "workspace_id")
    op.drop_table("profile_jobs")
    op.drop_table("source_subscriptions")
    op.drop_table("memberships")
    op.drop_table("workspaces")
    # The jobpulse_app role is left in place (roles are cluster-wide and may be shared).
