"""Idempotent demo seeding: ``python -m jobpulse.seed seed.yaml``.

Creates/updates the primary profile and upserts sources by (kind, name). Polling
workflows start automatically when the worker boots (or on the next PATCH/sync).
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from jobpulse.core.aio import run
from jobpulse.core.config import get_settings
from jobpulse.core.logging import configure_logging
from jobpulse.db.session import transaction
from jobpulse.repositories.profiles import ProfileRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.services.context import AppContext
from jobpulse_core.domain.models import EligibilityPolicy, Seniority, SourceDefinition

logger = structlog.get_logger(__name__)
MAX_SEED_BYTES = 256 * 1024


class SeedProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = "Owner"
    target_roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    years_experience: float = 0
    seniority: Seniority = Seniority.MID
    timezone: str = "IST"
    summary: str = ""
    policy: EligibilityPolicy = Field(default_factory=EligibilityPolicy)


class SeedSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    definition: SourceDefinition
    poll_interval_seconds: int = 900


class SeedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: SeedProfile | None = None
    sources: list[SeedSource] = Field(default_factory=list)


def load_seed(path: Path) -> SeedFile:
    if path.stat().st_size > MAX_SEED_BYTES:
        msg = f"seed file too large: {path}"
        raise ValueError(msg)
    payload: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return SeedFile.model_validate(payload)


async def apply_seed(ctx: AppContext, seed: SeedFile) -> tuple[bool, int]:
    async with transaction(ctx.sessions) as session:
        if seed.profile is not None:
            profile = await ProfileRepository(session).get_or_create_primary()
            data = seed.profile
            profile.display_name = data.display_name
            profile.target_roles = data.target_roles
            profile.skills = data.skills
            profile.years_experience = Decimal(str(data.years_experience))
            profile.seniority = data.seniority.value
            profile.timezone = data.timezone
            profile.summary = data.summary
            profile.policy = data.policy.model_dump(mode="json")
            profile.embedding = None
            profile.embedding_hash = None

        repo = SourceRepository(session)
        created = 0
        for item in seed.sources:
            definition = item.definition
            if await repo.get_by_kind_name(definition.kind.value, item.name) is not None:
                continue
            company = await repo.upsert_company(name=definition.company_name, domain=definition.company_domain)
            await repo.create(
                company_id=company.id,
                name=item.name,
                kind=definition.kind.value,
                config=definition.model_dump(mode="json"),
                poll_interval_seconds=item.poll_interval_seconds,
                min_poll_interval_seconds=300,
                max_poll_interval_seconds=3600,
            )
            created += 1
    return seed.profile is not None, created


async def _main(path: Path) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    async with AppContext.create(settings) as ctx:
        profile_updated, created = await apply_seed(ctx, load_seed(path))
    logger.info("seed.applied", profile_updated=profile_updated, sources_created=created)


def main() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: python -m jobpulse.seed <seed.yaml>\n")
        sys.exit(2)
    try:
        run(_main(Path(sys.argv[1])))
    except (OSError, ValueError, PydanticValidationError, yaml.YAMLError) as exc:
        sys.stderr.write(f"seed failed: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
