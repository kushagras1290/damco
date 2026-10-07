"""/api/v1/applications - application tracking."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from jobpulse.api.deps import Paging, Session
from jobpulse.api.mappers import application_out
from jobpulse.api.schemas import ApplicationOut, ApplicationPatch, ApplicationStatus, Page
from jobpulse.core.errors import NotFoundError
from jobpulse.core.security import Owner, Reader
from jobpulse.repositories.activity import ApplicationRepository, AuditRepository

router = APIRouter(prefix="/api/v1/applications", tags=["applications"])


@router.get("", response_model=Page[ApplicationOut])
async def list_applications(
    _: Reader,
    session: Session,
    paging: Paging,
    status: ApplicationStatus | None = None,
) -> Page[ApplicationOut]:
    rows, total = await ApplicationRepository(session).list(status=status, limit=paging.limit, offset=paging.offset)
    return Page(items=[application_out(r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.patch("/{application_id}", response_model=ApplicationOut)
async def update_application(
    application_id: uuid.UUID,
    body: ApplicationPatch,
    principal: Owner,
    session: Session,
) -> ApplicationOut:
    repo = ApplicationRepository(session)
    application = await repo.get(application_id)
    if application is None:
        raise NotFoundError("application not found")
    changes = body.model_dump(exclude_unset=True)
    for field, value in changes.items():
        if field == "notes" and value is None:
            continue
        setattr(application, field, value)
    await session.flush()
    await AuditRepository(session).record(
        actor=principal.actor,
        action="application.update",
        entity_type="application",
        entity_id=str(application_id),
        payload={"fields": sorted(changes)},
    )
    refreshed = await repo.get(application_id)
    if refreshed is None:
        raise NotFoundError("application not found")
    return application_out(refreshed)
