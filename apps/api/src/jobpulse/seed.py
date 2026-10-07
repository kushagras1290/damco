"""Idempotent demo seeding: ``python -m jobpulse.seed seed.yaml``.

Creates/updates the primary profile and upserts sources by (kind, name), then ensures
the (idempotent) polling workflow runs for every enabled seeded source. If Temporal is unreachable the seed
still succeeds: the worker starts polling for every enabled source when it boots.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
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
from jobpulse.services.temporal import WorkflowServiceError, connect, ensure_polling
from jobpulse_core.domain.models import EligibilityPolicy, Seniority, SourceDefinition, SourceKind

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
    poll_interval_seconds: int = 300
    min_poll_interval_seconds: int = 120


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


@dataclass(frozen=True, slots=True)
class SeedResult:
    profile_updated: bool
    created_source_ids: list[str] = field(default_factory=list)
    # Every enabled source named in the seed, new or pre-existing: these should be polling.
    enabled_source_ids: list[str] = field(default_factory=list)


async def apply_seed(ctx: AppContext, seed: SeedFile) -> SeedResult:
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
        created: list[str] = []
        enabled: list[str] = []
        for item in seed.sources:
            definition = item.definition
            if definition.kind is SourceKind.DEMO and not ctx.settings.demo_mode:
                msg = f"seed source {item.name!r} is a demo source; enable DEMO_MODE to seed it"
                raise ValueError(msg)
            existing = await repo.get_by_kind_name(definition.kind.value, item.name)
            if existing is not None:
                if existing.enabled:
                    enabled.append(str(existing.id))
                continue
            company = await repo.upsert_company(name=definition.company_name, domain=definition.company_domain)
            source = await repo.create(
                company_id=company.id,
                name=item.name,
                kind=definition.kind.value,
                config=definition.model_dump(mode="json"),
                poll_interval_seconds=item.poll_interval_seconds,
                min_poll_interval_seconds=item.min_poll_interval_seconds,
                max_poll_interval_seconds=3600,
            )
            created.append(str(source.id))
            enabled.append(str(source.id))
    return SeedResult(profile_updated=seed.profile is not None, created_source_ids=created, enabled_source_ids=enabled)


async def start_polling(ctx: AppContext, source_ids: list[str]) -> int:
    """Best effort: returns how many polling workflows were started."""
    if not source_ids:
        return 0
    try:
        client = await connect(ctx.settings)
    except WorkflowServiceError as exc:
        logger.warning("seed.polling_deferred", reason=exc.message, sources=len(source_ids))
        return 0
    started = 0
    for source_id in source_ids:
        try:
            await ensure_polling(client, ctx.settings, source_id)
            started += 1
        except WorkflowServiceError as exc:
            logger.warning("seed.polling_failed", source_id=source_id, error=exc.message)
    return started


async def _main(path: Path) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    async with AppContext.create(settings) as ctx:
        result = await apply_seed(ctx, load_seed(path))
        polling = await start_polling(ctx, result.enabled_source_ids)
    logger.info(
        "seed.applied",
        profile_updated=result.profile_updated,
        sources_created=len(result.created_source_ids),
        polling_started=polling,
    )


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
