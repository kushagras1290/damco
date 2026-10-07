"""Profiles and users.

Profile queries are workspace-scoped by row-level security: "primary" means the oldest
profile in the current workspace, and new profiles are stamped with that workspace.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.db.models import Profile
from jobpulse_core.domain.models import CandidateProfile, EligibilityPolicy, Seniority

DEFAULT_DISPLAY_NAME = "Owner"


def to_domain(profile: Profile) -> CandidateProfile:
    return CandidateProfile(
        display_name=profile.display_name,
        target_roles=list(profile.target_roles or []),
        skills=list(profile.skills or []),
        years_experience=float(profile.years_experience or 0),
        seniority=Seniority(profile.seniority),
        home_country=profile.home_country,
        timezone=profile.timezone,
        policy=EligibilityPolicy.model_validate(profile.policy) if profile.policy else EligibilityPolicy(),
        summary=profile.summary or "",
    )


class ProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_primary(self, *, for_update: bool = False) -> Profile | None:
        statement = select(Profile).order_by(Profile.created_at.asc()).limit(1)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_or_create_primary(self) -> Profile:
        existing = await self.get_primary(for_update=True)
        if existing is not None:
            return existing
        profile = Profile(
            display_name=DEFAULT_DISPLAY_NAME,
            target_roles=[],
            skills=[],
            years_experience=Decimal(0),
            policy=EligibilityPolicy().model_dump(mode="json"),
        )
        self._session.add(profile)
        await self._session.flush()
        return profile

    async def set_embedding(self, profile: Profile, *, vector: list[float], model: str, content_hash: str) -> None:
        profile.embedding = vector
        profile.embedding_model = model
        profile.embedding_hash = content_hash
        await self._session.flush()
