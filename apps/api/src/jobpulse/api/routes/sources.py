"""/api/v1/sources - the job boards a workspace follows (shared, deduplicated catalogue sources)."""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, BackgroundTasks, Response, status
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client

from jobpulse.api.deps import Ctx, Paging, Temporal
from jobpulse.api.mappers import source_out
from jobpulse.api.schemas import ActionAccepted, Page, SourceCreate, SourceOut, SourcePatch
from jobpulse.api.tenancy import Admin, Session, Viewer, system_section
from jobpulse.core.config import Settings
from jobpulse.core.errors import ConflictError, NotFoundError
from jobpulse.db.models import Source, SourceSubscription
from jobpulse.repositories.activity import AuditRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.repositories.tenancy import SubscriptionRepository
from jobpulse.services import temporal as temporal_service
from jobpulse.services.accounts import Account, WorkspaceRole
from jobpulse.services.plans import PlanLimitError, limits_for, require_capacity
from jobpulse.services.temporal import WorkflowServiceError
from jobpulse_core.domain.models import SourceDefinition, SourceKind
from jobpulse_core.errors import SourceFetchError, UnsafeUrlError, ValidationError
from jobpulse_core.ingestion.http import SafeHttpClient
from jobpulse_core.sources import required_hosts

router = APIRouter(prefix="/api/v1/sources", tags=["sources"])
logger = structlog.get_logger(__name__)
INTERVAL_FIELDS = ("poll_interval_seconds", "min_poll_interval_seconds", "max_poll_interval_seconds")


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


async def _source_for_workspace(session: Session, source_id: uuid.UUID) -> tuple[Source, SourceSubscription]:
    """The source and this workspace's subscription; 404 if the workspace does not follow it."""
    subscription = await SubscriptionRepository(session).subscription(source_id)
    source = await SourceRepository(session).get(source_id, for_update=True) if subscription else None
    if source is None or subscription is None:
        raise NotFoundError("source not found")
    return source, subscription


async def _refresh_activity(session: Session, account: Account, source: Source) -> tuple[bool, int]:
    """Recompute whether the shared board should poll (any unpaused follower anywhere).

    Returns (polling-state changed, total followers). Follower counts span workspaces, so this
    runs in a short system-scope section and only ever returns aggregates.
    """
    async with system_section(session, account):
        total, active = await SubscriptionRepository(session).follower_counts(source.id)
    should_poll = active > 0
    changed = source.enabled != should_poll
    if changed:
        source.enabled = should_poll
        if should_poll:
            source.consecutive_failures = 0
            source.circuit_open_until = None
        await session.flush()
    return changed, total


async def _out(session: Session, source: Source, *, paused: bool, editable: bool | None) -> SourceOut:
    counts = await SourceRepository(session).job_counts([source.id])
    return source_out(source, counts.get(source.id, 0), paused=paused, editable=editable)


@router.get("", response_model=Page[SourceOut])
async def list_sources(_: Viewer, session: Session, paging: Paging) -> Page[SourceOut]:
    subscriptions = SubscriptionRepository(session)
    rows = await subscriptions.subscribed_sources(limit=paging.limit, offset=paging.offset)
    counts = await SourceRepository(session).job_counts([source.id for source, _ in rows])
    return Page(
        items=[source_out(source, counts.get(source.id, 0), paused=paused, editable=None) for source, paused in rows],
        total=await subscriptions.count(),
        limit=paging.limit,
        offset=paging.offset,
    )


@router.get("/{source_id}", response_model=SourceOut)
async def get_source(source_id: uuid.UUID, _: Viewer, session: Session) -> SourceOut:
    subscription = await SubscriptionRepository(session).subscription(source_id)
    source = await SourceRepository(session).get(source_id) if subscription else None
    if source is None or subscription is None:
        raise NotFoundError("source not found")
    return await _out(session, source, paused=subscription.paused, editable=None)


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
async def create_source(
    body: SourceCreate,
    account: Admin,
    session: Session,
    ctx: Ctx,
    client: Temporal,
    background: BackgroundTasks,
) -> SourceOut:
    """Add a board. If any workspace already follows the same board, follow that shared source."""
    definition = await _validate_definition(body, ctx)
    repo = SourceRepository(session)
    subscriptions = SubscriptionRepository(session)
    plan = account.require(WorkspaceRole.ADMIN).plan
    limits = limits_for(plan)
    require_capacity(plan, what="followed boards", used=await subscriptions.count(), limit=limits.followed_sources)
    # New boards poll no faster than the plan allows (raised, not rejected, so defaults just work).
    floor = limits.min_poll_interval_seconds
    min_interval = max(body.min_poll_interval_seconds, floor)
    max_interval = max(body.max_poll_interval_seconds, min_interval)
    poll_interval = min(max(body.poll_interval_seconds, min_interval), max_interval)
    source = await repo.get_by_board(definition, for_update=True)
    created = source is None
    if source is None:
        company = await repo.upsert_company(name=definition.company_name, domain=definition.company_domain)
        try:
            async with session.begin_nested():
                source = await repo.create(
                    company_id=company.id,
                    name=body.name,
                    definition=definition,
                    poll_interval_seconds=poll_interval,
                    min_poll_interval_seconds=min_interval,
                    max_poll_interval_seconds=max_interval,
                )
        except IntegrityError:
            # Another workspace added the same board concurrently: follow theirs.
            source = await repo.get_by_board(definition, for_update=True)
            created = False
            if source is None:
                raise ConflictError("source conflicts with an existing source") from None
    elif await subscriptions.subscription(source.id) is not None:
        raise ConflictError("this workspace already follows this board")
    await subscriptions.subscribe(source.id)
    changed, total = await _refresh_activity(session, account, source)
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="source.create" if created else "source.follow",
        entity_type="source",
        entity_id=str(source.id),
        payload={"kind": source.kind, "name": source.name},
    )
    if created or changed:
        background.add_task(_apply_polling, client, ctx.settings, str(source.id), enabled=True)
    return await _out(session, source, paused=False, editable=total <= 1 or account.principal.platform_admin)


@router.patch("/{source_id}", response_model=SourceOut)
async def update_source(
    source_id: uuid.UUID,
    body: SourcePatch,
    account: Admin,
    session: Session,
    ctx: Ctx,
    client: Temporal,
    background: BackgroundTasks,
) -> SourceOut:
    """``enabled`` pauses/resumes the board for this workspace only. Name and polling intervals
    are shared settings: editable by the board's sole follower or a platform admin."""
    source, subscription = await _source_for_workspace(session, source_id)
    changes = body.model_dump(exclude_unset=True)
    shared_fields = {key: value for key, value in changes.items() if key != "enabled"}
    if shared_fields:
        async with system_section(session, account):
            total, _ = await SubscriptionRepository(session).follower_counts(source.id)
        if total > 1 and not account.principal.platform_admin:
            raise ConflictError("this board is shared with other workspaces; only pausing is available")
        floor = limits_for(account.require(WorkspaceRole.ADMIN).plan).min_poll_interval_seconds
        requested = [shared_fields[key] for key in INTERVAL_FIELDS if key in shared_fields]
        if any(value < floor for value in requested) and not account.principal.platform_admin:
            raise PlanLimitError(
                f"your plan polls at most every {floor // 60} minutes; upgrade for faster polling",
                context={"min_poll_interval_seconds": floor},
            )
        low = shared_fields.get("min_poll_interval_seconds", source.min_poll_interval_seconds)
        high = shared_fields.get("max_poll_interval_seconds", source.max_poll_interval_seconds)
        current = shared_fields.get("poll_interval_seconds", min(max(source.poll_interval_seconds, low), high))
        if not low <= current <= high:
            raise ValidationError("require min_poll_interval <= poll_interval <= max_poll_interval")
        for field, value in shared_fields.items():
            setattr(source, field, value)
        source.poll_interval_seconds = current
    if "enabled" in changes:
        subscription.paused = not changes["enabled"]
    await session.flush()
    changed, total = await _refresh_activity(session, account, source)
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="source.update",
        entity_type="source",
        entity_id=str(source_id),
        payload=changes,
    )
    if changed:
        background.add_task(_apply_polling, client, ctx.settings, str(source_id), enabled=source.enabled)
    return await _out(
        session, source, paused=subscription.paused, editable=total <= 1 or account.principal.platform_admin
    )


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unfollow_source(
    source_id: uuid.UUID,
    account: Admin,
    session: Session,
    ctx: Ctx,
    client: Temporal,
    background: BackgroundTasks,
) -> Response:
    """Stop following a board. Its jobs stay in the shared catalogue; polling stops when the
    last workspace leaves."""
    source, subscription = await _source_for_workspace(session, source_id)
    await SubscriptionRepository(session).unsubscribe(subscription)
    changed, _ = await _refresh_activity(session, account, source)
    await AuditRepository(session).record(
        actor=account.principal.actor, action="source.unfollow", entity_type="source", entity_id=str(source_id)
    )
    if changed:
        background.add_task(_apply_polling, client, ctx.settings, str(source_id), enabled=False)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{source_id}/sync", response_model=ActionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def sync_source(
    source_id: uuid.UUID,
    account: Admin,
    session: Session,
    ctx: Ctx,
    client: Temporal,
) -> ActionAccepted:
    source, subscription = await _source_for_workspace(session, source_id)
    if subscription.paused or not source.enabled:
        raise ConflictError("source is paused")
    workflow_id = await temporal_service.trigger_sync(client, ctx.settings, str(source_id))
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="source.sync",
        entity_type="source",
        entity_id=str(source_id),
        payload={"workflow_id": workflow_id},
    )
    logger.info("source.sync_triggered", source_id=str(source_id), workflow_id=workflow_id)
    return ActionAccepted(workflow_id=workflow_id)
