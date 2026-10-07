"""accounts: sign-in identities, invitations, provider-neutral users (+ RLS).

Users become provider-neutral (identities link GitHub / Google / Microsoft / email
subjects to them). User rows are visible only in system scope or to members of a
workspace the user belongs to; identities only in system scope (sign-in resolution).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-08 09:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_WORKSPACE = sa.text("NULLIF(current_setting('app.workspace_id', true), '')::uuid")

ENABLE_RLS = """
ALTER TABLE identities ENABLE ROW LEVEL SECURITY;
ALTER TABLE identities FORCE ROW LEVEL SECURITY;
CREATE POLICY system_only ON identities
    USING (current_setting('app.system_scope', true) = 'on')
    WITH CHECK (current_setting('app.system_scope', true) = 'on');

ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
-- memberships is itself RLS-filtered to the current workspace, so this exposes exactly the
-- current workspace's members (names for the members page), never other tenants' users.
CREATE POLICY visible_users ON users
    USING (
        current_setting('app.system_scope', true) = 'on'
        OR EXISTS (SELECT 1 FROM memberships m WHERE m.user_id = users.id)
    )
    WITH CHECK (current_setting('app.system_scope', true) = 'on');

ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitations FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON invitations
    USING (
        current_setting('app.system_scope', true) = 'on'
        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
    )
    WITH CHECK (
        current_setting('app.system_scope', true) = 'on'
        OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
    );
"""

DISABLE_RLS = """
DROP POLICY IF EXISTS tenant_isolation ON invitations;
DROP POLICY IF EXISTS visible_users ON users;
ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
ALTER TABLE users DISABLE ROW LEVEL SECURITY;
"""


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(length=200), server_default="", nullable=False))
    op.add_column("users", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True))
    op.alter_column("users", "github_login", existing_type=sa.String(length=100), nullable=True)
    op.execute("UPDATE users SET display_name = github_login WHERE github_login IS NOT NULL")

    op.create_table(
        "identities",
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "provider IN ('github', 'google', 'microsoft', 'email')", name=op.f("ck_identities_provider_valid")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_identities_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("provider", "subject", name=op.f("pk_identities")),
    )
    op.create_index(op.f("ix_identities_user_id"), "identities", ["user_id"], unique=False)

    op.create_table(
        "invitations",
        sa.Column("id", sa.UUID(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("workspace_id", sa.UUID(), server_default=CURRENT_WORKSPACE, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("role", sa.String(length=20), server_default="member", nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by", sa.UUID(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.UUID(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("role IN ('owner', 'admin', 'member')", name=op.f("ck_invitations_role_valid")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_invitations_workspace_id_workspaces"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["invited_by"], ["users.id"], name=op.f("fk_invitations_invited_by_users"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["accepted_by"], ["users.id"], name=op.f("fk_invitations_accepted_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_invitations_token_hash")),
    )
    op.create_index(op.f("ix_invitations_workspace_id"), "invitations", ["workspace_id"], unique=False)
    op.execute(ENABLE_RLS)


def downgrade() -> None:
    op.execute(DISABLE_RLS)
    op.drop_table("invitations")
    op.drop_table("identities")
    op.execute("UPDATE users SET github_login = 'legacy-' || id::text WHERE github_login IS NULL")
    op.alter_column("users", "github_login", existing_type=sa.String(length=100), nullable=False)
    op.drop_column("users", "last_seen_at")
    op.drop_column("users", "display_name")
