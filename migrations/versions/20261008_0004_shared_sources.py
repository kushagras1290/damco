"""shared sources: dedupe by (kind, locator); per-workspace pause on subscriptions.

A job board is identified by what it points at (ATS token or normalized URL), not by a
display name, so workspaces adding the same board share one poller.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 12:00:00+00:00
"""

from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Frozen copy of jobpulse_core.sources.locator at the time of this migration.
TOKEN_KINDS = frozenset({"greenhouse", "lever", "ashby"})
DEFAULT_PORTS = {"http": 80, "https": 443}


def _locator(kind: str, config: dict[str, Any]) -> str:
    if kind == "demo":
        return "demo"
    if kind in TOKEN_KINDS:
        return str(config.get("board_token") or "").strip().lower()
    parts = urlsplit(str(config.get("url") or "").strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    netloc = host if parts.port is None or DEFAULT_PORTS.get(scheme) == parts.port else f"{host}:{parts.port}"
    return urlunsplit((scheme, netloc, parts.path.rstrip("/"), parts.query, ""))


def upgrade() -> None:
    op.add_column("sources", sa.Column("locator", sa.String(length=2048), nullable=True))
    sources = sa.table(
        "sources",
        sa.column("id", sa.UUID()),
        sa.column("kind", sa.String()),
        sa.column("config", sa.JSON()),
        sa.column("locator", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    bind = op.get_bind()
    seen: set[tuple[str, str]] = set()
    rows = bind.execute(sa.select(sources.c.id, sources.c.kind, sources.c.config).order_by(sources.c.created_at))
    for source_id, kind, config in rows.all():
        locator = _locator(kind, config or {})
        if (kind, locator) in seen:
            # Pre-existing duplicate board: keep it addressable; the oldest one is canonical.
            locator = f"{locator}#duplicate-{source_id}"
        seen.add((kind, locator))
        bind.execute(sa.update(sources).where(sources.c.id == source_id).values(locator=locator))
    op.alter_column("sources", "locator", nullable=False)
    op.drop_constraint("uq_sources_kind_name", "sources", type_="unique")
    op.create_unique_constraint(op.f("uq_sources_kind_locator"), "sources", ["kind", "locator"])
    op.add_column("source_subscriptions", sa.Column("paused", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("source_subscriptions", "paused")
    op.drop_constraint(op.f("uq_sources_kind_locator"), "sources", type_="unique")
    op.create_unique_constraint("uq_sources_kind_name", "sources", ["kind", "name"])
    op.drop_column("sources", "locator")
