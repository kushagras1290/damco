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
from sqlalchemy import text

from jobpulse.core.config import Settings
from jobpulse.services.context import AppContext
from jobpulse_core.ingestion.http import HttpClientConfig, SafeHttpClient
from tests.auth_helpers import JWKS_JSON, OWNER_ID

ROOT = Path(__file__).resolve().parents[2]
PG_IMAGE = "pgvector/pgvector:pg18"
TRUNCATE = (
    "TRUNCATE audit_events, applications, notifications, match_scores, job_intelligence, "
    "eligibility_decisions, job_versions, raw_snapshots, workflow_runs, jobs, source_checkpoints, "
    "sources, companies, profiles, users RESTART IDENTITY CASCADE"
)


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
        rate_limit_per_minute=1000,
        trusted_proxy_count=0,
        openai_api_key=None,
        webhook_signing_secret="whsec-test",  # type: ignore[arg-type]
    )


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
    try:
        yield context
    finally:
        await context.aclose()
