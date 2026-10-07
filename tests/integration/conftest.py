"""Integration fixtures: real PostgreSQL 18 + pgvector via Testcontainers, real migrations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import text

from jobpulse.core.config import Settings
from jobpulse.db.models import DEFAULT_WORKSPACE_ID
from jobpulse.services.context import AppContext
from jobpulse_core.ingestion.http import HttpClientConfig, SafeHttpClient
from tests.auth_helpers import JWKS_JSON, OWNER_ID

ROOT = Path(__file__).resolve().parents[2]
PG_IMAGE = "pgvector/pgvector:pg18"
REDIS_IMAGE = "redis:8.8.3-alpine"
REDIS_TEST_PASSWORD = "jobpulse-test"
TRUNCATE = (
    "TRUNCATE audit_events, applications, notifications, match_scores, job_intelligence, "
    "eligibility_decisions, profile_jobs, job_versions, raw_snapshots, workflow_runs, jobs, "
    "source_checkpoints, source_subscriptions, sources, companies, profiles, memberships, users, "
    "workspaces RESTART IDENTITY CASCADE"
)
# Recreated after every truncate: single-workspace mode acts on it (as migration 0002 does).
RESET_DEFAULT_WORKSPACE = "INSERT INTO workspaces (id, name, slug, plan) VALUES (:id, 'Default', 'default', 'team')"


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        probe = subprocess.run([docker, "info"], capture_output=True, timeout=20, check=False)  # noqa: S603 - fixed argv
    except OSError, subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    external = os.environ.get("JOBPULSE_TEST_DATABASE_URL")
    if external:
        yield external
        return
    if not _docker_available():
        pytest.skip("Docker not available for Testcontainers")
    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415 - optional heavy import

    with PostgresContainer(
        PG_IMAGE, username="jobpulse", password="jobpulse", dbname="jobpulse", driver="psycopg"
    ) as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    external = os.environ.get("JOBPULSE_TEST_REDIS_URL")
    if external:
        yield external
        return
    if not _docker_available():
        pytest.skip("Docker not available for Testcontainers")
    from testcontainers.community.redis import RedisContainer  # noqa: PLC0415 - optional heavy import

    with RedisContainer(REDIS_IMAGE, password=REDIS_TEST_PASSWORD) as container:
        host = container.get_container_host_ip()
        port = container.get_exposed_port(container.port)
        yield f"redis://:{REDIS_TEST_PASSWORD}@{host}:{port}/0"


@pytest.fixture(scope="session")
def migrated(database_url: str) -> str:
    env = {**os.environ, "DATABASE_URL": database_url}
    for attempt in range(10):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0:
            return database_url
        time.sleep(1 + attempt)
    pytest.fail(f"alembic upgrade failed:\n{result.stderr}")


@pytest.fixture
def settings(migrated: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=migrated,  # type: ignore[arg-type]
        api_jwt_jwks=JWKS_JSON,
        owner_github_ids=[OWNER_ID],
        environment="test",
        local_storage_path=tmp_path / "snapshots",
        log_json=False,
        rate_limit_anonymous=10_000,
        rate_limit_authenticated=10_000,
        rate_limit_writes=10_000,
        rate_limit_stream_connects=10_000,
        trusted_proxy_count=0,
        openai_api_key=None,
        webhook_signing_secret="whsec-test",  # type: ignore[arg-type]
    )


@pytest.fixture
async def redis_settings(settings: Settings, redis_url: str) -> Settings:
    """Settings wired to a real (flushed) Redis: shared limiter, cache and idempotency."""
    from redis.asyncio import Redis  # noqa: PLC0415 - only needed by Redis-backed tests

    client = Redis.from_url(redis_url)
    try:
        await client.flushdb()
    finally:
        await client.aclose()
    return settings.model_copy(update={"redis_url": SecretStr(redis_url)})


class TestHttpContext(AppContext):
    """AppContext whose outbound clients skip DNS (traffic is mocked by respx)."""

    def source_http(self, extra_hosts: list[str]) -> SafeHttpClient:
        return SafeHttpClient(HttpClientConfig(skip_dns_check=True, respect_robots_txt=False))


@pytest.fixture
async def ctx(settings: Settings) -> AsyncIterator[AppContext]:
    base = AppContext.create(settings)
    context = TestHttpContext(
        settings=base.settings,
        engine=base.engine,
        sessions=base.sessions,
        store=base.store,
        intelligence=None,
        notify_http=base.notify_http,
    )
    async with context.engine.begin() as conn:
        await conn.execute(text(TRUNCATE))
        await conn.execute(text(RESET_DEFAULT_WORKSPACE), {"id": DEFAULT_WORKSPACE_ID})
    try:
        yield context
    finally:
        await context.aclose()
