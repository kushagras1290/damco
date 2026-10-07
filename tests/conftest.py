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

FIXTURES = Path(__file__).parent / "fixtures"

# Settings are validated at import time by some modules; provide safe test defaults.
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://jobpulse:jobpulse@localhost:5432/jobpulse_test")
os.environ.setdefault("API_JWT_SECRET", "test-secret-test-secret-test-secret-0123")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_JSON", "false")


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]] | None:
    """psycopg async needs a selector loop on Windows (Proactor is unsupported)."""
    factory = loop_factory()
    return {"selector": factory} if factory is not None else None


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
