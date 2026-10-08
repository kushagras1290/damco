"""billing: subscription state on workspaces + an idempotent provider event log.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08 15:00:00+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SYSTEM_ONLY_RLS = """
ALTER TABLE billing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_events FORCE ROW LEVEL SECURITY;
CREATE POLICY system_only ON billing_events
    USING (current_setting('app.system_scope', true) = 'on')
    WITH CHECK (current_setting('app.system_scope', true) = 'on');
"""


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("billing_provider", sa.String(length=20), nullable=True))
    op.add_column("workspaces", sa.Column("subscription_id", sa.String(length=100), nullable=True))
    op.add_column("workspaces", sa.Column("subscription_status", sa.String(length=20), nullable=True))
    op.add_column("workspaces", sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True))
    op.add_column("workspaces", sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint(op.f("uq_workspaces_subscription_id"), "workspaces", ["subscription_id"])
    op.create_check_constraint(
        op.f("ck_workspaces_billing_provider_valid"),
        "workspaces",
        "billing_provider IS NULL OR billing_provider IN ('razorpay', 'stripe')",
    )
    op.create_table(
        "billing_events",
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("event_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_billing_events_workspace_id_workspaces"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("provider", "event_id", name=op.f("pk_billing_events")),
    )
    op.execute(SYSTEM_ONLY_RLS)


def downgrade() -> None:
    op.drop_table("billing_events")
    op.drop_constraint(op.f("ck_workspaces_billing_provider_valid"), "workspaces", type_="check")
    op.drop_constraint(op.f("uq_workspaces_subscription_id"), "workspaces", type_="unique")
    for column in ("grace_until", "current_period_end", "subscription_status", "subscription_id", "billing_provider"):
        op.drop_column("workspaces", column)
