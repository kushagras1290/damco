"""/api/v1/sources - manage job sources and trigger ingestion."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client

from jobpulse.api.deps import Ctx, Paging, Temporal
from jobpulse.api.mappers import source_out
from jobpulse.api.schemas import ActionAccepted, Page, SourceCreate, SourceOut, SourcePatch
from jobpulse.api.tenancy import Session
from jobpulse.core.config import Settings
from jobpulse.core.errors import ConflictError, NotFoundError
from jobpulse.core.security import Owner, Reader
from jobpulse.repositories.activity import AuditRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.repositories.tenancy import SubscriptionRepository
from jobpulse.services import temporal as temporal_service
from jobpulse.services.temporal import WorkflowServiceError
from jobpulse_core.domain.models import SourceDefinition, SourceKind
from jobpulse_core.errors import SourceFetchError, UnsafeUrlError, ValidationError
from jobpulse_core.ingestion.http import SafeHttpClient
from jobpulse_core.sources import required_hosts

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])
logger = structlog.get_logger(__name__)


async def _apply_polling(client: Client, settings: Settings, source_id: str, *, enabled: bool) -> None:
    """Runs as a background task, i.e. after the request transaction has committed,
    so the polling workflow always observes the new source state."""
    try:
        if enabled:
            await temporal_service.ensure_polling(client, settings, source_id)
        else:
            await temporal_service.stop_polling(client, source_id)
    except WorkflowServiceError as exc:
        # The worker re-creates polling workflows for enabled sources on startup.
        logger.warning("source.polling_update_failed", source_id=source_id, enabled=enabled, error=exc.message)


async def _validate_definition(body: SourceCreate, ctx: Ctx) -> SourceDefinition:
    if body.kind is SourceKind.DEMO and not ctx.settings.demo_mode:
        raise ValidationError("demo sources are only available when DEMO_MODE is enabled")
    try:
        definition = SourceDefinition(
            kind=body.kind,
            company_name=body.company_name,
            company_domain=body.company_domain.lower(),
            board_token=body.board_token,
            url=body.url,
            field_map=body.field_map,
        )
    except PydanticValidationError as exc:
        raise ValidationError(exc.errors(include_url=False)[0]["msg"]) from exc
    if definition.url is not None:
        # SSRF guard at write time: reject private / internal targets before storing.
        async with SafeHttpClient(ctx.settings.http_client_config(extra_hosts=required_hosts(definition))) as http:
            try:
                await http.validate_url(str(definition.url))
            except UnsafeUrlError as exc:
                raise ValidationError(f"source url rejected: {exc.message}") from exc
            except SourceFetchError as exc:
                raise ValidationError(f"source url could not be resolved: {exc.message}") from exc
    return definition


@router.get("", response_model=Page[SourceOut])
async def list_sources(_: Reader, session: Session, paging: Paging) -> Page[SourceOut]:
    repo = SourceRepository(session)
    subscriptions = SubscriptionRepository(session)
    rows = await subscriptions.subscribed_sources(limit=paging.limit, offset=paging.offset)
    counts = await repo.job_counts([row.id for row in rows])
    return Page(
        items=[source_out(row, counts.get(row.id, 0)) for row in rows],
        total=await subscriptions.count(),
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: uuid.UUID, _: Reader, session: Session) -> SourceOut:
    repo = SourceRepository(session)
    source = await repo.get(source_id)
    if source is None:
        raise NotFoundError("source not found")
    counts = await repo.job_counts([source.id])
    return source_out(source, counts.get(source.id, 0))


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceCreate,
    principal: Owner,
    session: Session,
    ctx: Ctx,
    client: Temporal,
    background: BackgroundTasks,
) -> SourceOut:
    definition = await _validate_definition(body, ctx)
    repo = SourceRepository(session)
    if await repo.get_by_kind_name(definition.kind.value, body.name) is not None:
        raise ConflictError("a source with this kind and name already exists")
    company = await repo.upsert_company(name=definition.company_name, domain=definition.company_domain)
    try:
        source = await repo.create(
            company_id=company.id,
            name=body.name,
            kind=definition.kind.value,
            config=definition.model_dump(mode="json"),
            poll_interval_seconds=body.poll_interval_seconds,
            min_poll_interval_seconds=body.min_poll_interval_seconds,
            max_poll_interval_seconds=body.max_poll_interval_seconds,
        )
    except IntegrityError as exc:
        raise ConflictError("source conflicts with an existing source") from exc
    await AuditRepository(session).record(
        actor=principal.actor,
        action="source.create",
        entity_type="source",
        entity_id=str(source.id),
        payload={"kind": source.kind, "name": source.name},
    )
    await SubscriptionRepository(session).subscribe(source.id)  # the creator's workspace follows it
    background.add_task(_apply_polling, client, ctx.settings, str(source.id), enabled=True)
    return source_out(source, 0)


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: uuid.UUID,
    body: SourcePatch,
    principal: Owner,
    session: Session,
    ctx: Ctx,
    client: Temporal,
    background: BackgroundTasks,
) -> SourceOut:
    repo = SourceRepository(session)
    source = await repo.get(source_id, for_update=True)
    if source is None:
        raise NotFoundError("source not found")
    changes = body.model_dump(exclude_unset=True)
    low = changes.get("min_poll_interval_seconds", source.min_poll_interval_seconds)
    high = changes.get("max_poll_interval_seconds", source.max_poll_interval_seconds)
    current = changes.get("poll_interval_seconds", min(max(source.poll_interval_seconds, low), high))
    if not low <= current <= high:
        raise ValidationError("require min_poll_interval <= poll_interval <= max_poll_interval")
    for field, value in changes.items():
        setattr(source, field, value)
    source.poll_interval_seconds = current
    if changes.get("enabled") is True:
        source.consecutive_failures = 0
        source.circuit_open_until = None
    await session.flush()
    await AuditRepository(session).record(
        actor=principal.actor,
        action="source.update",
        entity_type="source",
        entity_id=str(source_id),
        payload=changes,
    )
    if "enabled" in changes:
        background.add_task(_apply_polling, client, ctx.settings, str(source_id), enabled=source.enabled)
    counts = await repo.job_counts([source.id])
    return source_out(source, counts.get(source.id, 0))


@router.post("/{source_id}/sync", response_model=ActionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def sync_source(
    source_id: uuid.UUID,
    principal: Owner,
    session: Session,
    ctx: Ctx,
    client: Temporal,
) -> ActionAccepted:
    source = await SourceRepository(session).get(source_id)
    if source is None:
        raise NotFoundError("source not found")
    if not source.enabled:
        raise ConflictError("source is disabled")
    workflow_id = await temporal_service.trigger_sync(client, ctx.settings, str(source_id))
    await AuditRepository(session).record(
        actor=principal.actor,
        action="source.sync",
        entity_type="source",
        entity_id=str(source_id),
        payload={"workflow_id": workflow_id},
    )
    logger.info("source.sync_triggered", source_id=str(source_id), workflow_id=workflow_id)
    return ActionAccepted(workflow_id=workflow_id)
