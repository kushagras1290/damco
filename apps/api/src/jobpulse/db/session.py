"""Async engine / session factory with bounded pool and statement timeouts."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from jobpulse.core.config import Settings
from jobpulse.db.tenancy import TenantScope, apply_scope, current_scope, validate_role

DEFAULT_PREPARE_THRESHOLD = 5
CONNECT_TIMEOUT_SECONDS = 10
POOL_RECYCLE_SECONDS = 1800
TENANT_ROLE_KEY = "tenant_role"


def create_engine(settings: Settings) -> AsyncEngine:
    pooled = not settings.use_prepared_statements  # behind PgBouncer (transaction pooling)
    connect_args: dict[str, Any] = {
        "connect_timeout": CONNECT_TIMEOUT_SECONDS,
        "application_name": settings.service_name,
        # None disables psycopg's automatic server-side prepares (PgBouncer-safe).
        "prepare_threshold": None if pooled else DEFAULT_PREPARE_THRESHOLD,
    }
    if not pooled:
        # Direct connection: a session-level default is cheapest.
        connect_args["options"] = f"-c statement_timeout={settings.db_statement_timeout_ms}"
    engine = create_async_engine(
        settings.sqlalchemy_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_pre_ping=True,
        pool_recycle=POOL_RECYCLE_SECONDS,
        connect_args=connect_args,
    )
    if pooled:
        # Session-level settings do not survive transaction pooling; SET LOCAL is
        # transaction-scoped, so every transaction still gets the timeout.
        timeout_sql = f"SET LOCAL statement_timeout = {int(settings.db_statement_timeout_ms)}"

        @event.listens_for(engine.sync_engine, "begin")
        def _apply_statement_timeout(connection: Any) -> None:
            connection.exec_driver_sql(timeout_sql)

    return engine


def create_session_factory(engine: AsyncEngine, *, tenant_role: str | None = None) -> async_sessionmaker[AsyncSession]:
    """``tenant_role``: the non-owner role every transaction switches to, so RLS applies."""
    return async_sessionmaker(
        engine, expire_on_commit=False, autoflush=False, info={TENANT_ROLE_KEY: validate_role(tenant_role)}
    )


@asynccontextmanager
async def transaction(
    factory: async_sessionmaker[AsyncSession], *, scope: TenantScope | None = None
) -> AsyncGenerator[AsyncSession]:
    """One transaction under a tenant scope (default: the caller's current scope).

    Commits on success, rolls back on any error.
    """
    async with factory() as session, session.begin():
        await apply_scope(session, scope or current_scope(), role=session.info.get(TENANT_ROLE_KEY))
        yield session


async def ping(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
