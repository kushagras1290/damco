"""Shared test fixtures and factories."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jobpulse.core.aio import loop_factory
from jobpulse_core.domain.models import (
    CandidateProfile,
    EligibilityPolicy,
    ExperienceRange,
    NormalizedJob,
    RawJob,
    RemotePolicy,
    Seniority,
)
from jobpulse_core.ingestion.normalize import normalize_job
from tests.auth_helpers import JWKS_JSON

FIXTURES = Path(__file__).parent / "fixtures"

# Settings are validated at import time by some modules; provide safe test defaults.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://jobpulse:jobpulse@localhost:5432/jobpulse_test")
os.environ.setdefault("API_JWT_JWKS", JWKS_JSON)
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_JSON", "false")


_SELECTOR_LOOP = loop_factory()

if _SELECTOR_LOOP is not None:
    # Defined only where needed: pytest-asyncio rejects a hook that returns no factories,
    # so on Linux/macOS the plugin's default loop is used.

    def pytest_asyncio_loop_factories(
        config: pytest.Config, item: pytest.Item
    ) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
        """psycopg async needs a selector loop on Windows (Proactor is unsupported)."""
        assert _SELECTOR_LOOP is not None
        return {"selector": _SELECTOR_LOOP}


def load_fixture(relative: str) -> Any:
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))


def fixture_bytes(relative: str) -> bytes:
    return (FIXTURES / relative).read_bytes()


@pytest.fixture
def policy() -> EligibilityPolicy:
    return EligibilityPolicy(
        allowed_locations=["India", "Worldwide"],
        allowed_work_models=[RemotePolicy.REMOTE],
        allowed_timezones=["IST", "GMT", "BST", "CET", "EET"],
        experience=ExperienceRange(min=3, max=6),
        excluded_regions=["US-only", "Canada-only"],
    )


@pytest.fixture
def profile(policy: EligibilityPolicy) -> CandidateProfile:
    return CandidateProfile(
        display_name="Test",
        target_roles=["Senior AI Engineer", "Backend Engineer"],
        skills=["Python", "FastAPI", "PostgreSQL", "LLM", "RAG", "Docker"],
        years_experience=5,
        seniority=Seniority.SENIOR,
        policy=policy,
    )


@pytest.fixture
def make_job() -> Callable[..., NormalizedJob]:
    def _make(
        *,
        title: str = "Senior AI Engineer",
        location: str | None = "Remote - Worldwide",
        description: str = "Remote-first team. 4+ years of experience with Python and FastAPI.",
        remote_hint: bool | None = None,
        published_at: datetime | None = None,
        external_id: str = "job-1",
    ) -> NormalizedJob:
        raw = RawJob(
            external_id=external_id,
            title=title,
            url=f"https://boards.example.com/acme/{external_id}",
            company_name="Acme",
            company_domain="acme.io",
            location=location,
            description_html=f"<p>{description}</p>",
            published_at=published_at or datetime(2026, 10, 1, tzinfo=UTC),
            remote_hint=remote_hint,
        )
        return normalize_job(raw)

    return _make
