"""email sign-in: single-use magic-link tokens (system scope only).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-08 18:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ONLY_RLS = """
ALTER TABLE email_login_tokens ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_login_tokens FORCE ROW LEVEL SECURITY;
CREATE POLICY system_only ON email_login_tokens
    USING (current_setting('app.system_scope', true) = 'on')
    WITH CHECK (current_setting('app.system_scope', true) = 'on');
"""


def upgrade() -> None:
    op.create_table(
        "email_login_tokens",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("token_hash", name=op.f("pk_email_login_tokens")),
    )
    op.create_index(op.f("ix_email_login_tokens_expires_at"), "email_login_tokens", ["expires_at"], unique=False)
    op.execute(SYSTEM_ONLY_RLS)


def downgrade() -> None:
    op.drop_table("email_login_tokens")
