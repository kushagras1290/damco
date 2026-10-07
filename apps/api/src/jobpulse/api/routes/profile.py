"""/api/v1/profile - the workspace's candidate profile and eligibility policy."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter

from jobpulse.api.deps import Ctx
from jobpulse.api.mappers import profile_out
from jobpulse.api.schemas import ProfileOut, ProfilePatch
from jobpulse.api.tenancy import Member, Session, Viewer
from jobpulse.repositories.activity import AuditRepository
from jobpulse.repositories.profiles import ProfileRepository
from jobpulse_core.errors import SourceFetchError, UnsafeUrlError, ValidationError
from jobpulse_core.ingestion.http import SafeHttpClient

router = APIRouter(prefix="/api/v1", tags=["profile"])

# Fields whose change invalidates the stored profile embedding.
EMBEDDING_FIELDS = frozenset({"target_roles", "skills", "years_experience", "seniority", "summary"})


@router.get("/profile", response_model=ProfileOut)
async def get_profile(_: Viewer, session: Session) -> ProfileOut:
    return profile_out(await ProfileRepository(session).get_or_create_primary())


@router.patch("/profile", response_model=ProfileOut)
async def update_profile(body: ProfilePatch, account: Member, session: Session, ctx: Ctx) -> ProfileOut:
    changes = body.model_dump(exclude_unset=True, mode="json")
    if changes.get("webhook_url"):
        # SSRF guard: never store a webhook pointing at internal infrastructure.
        async with SafeHttpClient(ctx.settings.http_client_config(robots=False)) as http:
            try:
                await http.validate_url(changes["webhook_url"])
            except UnsafeUrlError as exc:
                raise ValidationError(f"webhook_url rejected: {exc.message}") from exc
            except SourceFetchError as exc:
                raise ValidationError(f"webhook_url could not be resolved: {exc.message}") from exc

    profile = await ProfileRepository(session).get_or_create_primary()
    for field, raw in changes.items():
        value = raw
        if field == "years_experience" and raw is not None:
            value = Decimal(str(raw))
        if field in {"target_roles", "skills"} and raw is not None:
            value = list(dict.fromkeys(item.strip() for item in raw if item.strip()))
        setattr(profile, field, value)
    if EMBEDDING_FIELDS & changes.keys():
        profile.embedding = None
        profile.embedding_hash = None
    await session.flush()
    await session.refresh(profile)
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="profile.update",
        entity_type="profile",
        entity_id=str(profile.id),
        payload={"fields": sorted(changes)},
    )
    return profile_out(profile)
