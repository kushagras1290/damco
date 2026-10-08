"""/api/v1/auth/email/* (magic links) and workspace export / deletion."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, BackgroundTasks, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import select
from temporalio.client import Client

from jobpulse.api.deps import Ctx, Temporal
from jobpulse.api.tenancy import Session, WorkspaceOwner, system_section
from jobpulse.core.config import Settings
from jobpulse.core.errors import ConflictError, NotFoundError
from jobpulse.db.models import (
    DEFAULT_WORKSPACE_ID,
    Application,
    AuditEvent,
    EligibilityDecision,
    MatchScore,
    Membership,
    Notification,
    Profile,
    ProfileJob,
    Source,
    SourceSubscription,
    User,
    Workspace,
)
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE
from jobpulse.repositories.sources import SourceRepository
from jobpulse.repositories.tenancy import SubscriptionRepository
from jobpulse.services import email_login
from jobpulse.services import temporal as temporal_service
from jobpulse.services.accounts import WorkspaceRole
from jobpulse.services.billing import cancel as cancel_subscription
from jobpulse.services.razorpay import BillingProviderError
from jobpulse.services.temporal import WorkflowServiceError

router = APIRouter(prefix="/api/v1", tags=["lifecycle"])
logger = structlog.get_logger(__name__)

EXPORT_FORMAT_VERSION = 1


class EmailStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr


class EmailVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: Annotated[str, Field(min_length=20, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")]


class EmailVerified(BaseModel):
    subject: str
    email: str


class WorkspaceDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_name: Annotated[str, Field(min_length=1, max_length=200)]


@router.post("/auth/email/start", status_code=status.HTTP_202_ACCEPTED)
async def email_start(body: EmailStart, request: Request, ctx: Ctx) -> dict[str, str]:
    """Always 202 for a well-formed address: never reveals whether an account exists."""
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        await email_login.start(session, ctx.settings, request.app.state.redis, str(body.email))
    return {"status": "sent"}


@router.post("/auth/email/verify", response_model=EmailVerified)
async def email_verify(body: EmailVerify, ctx: Ctx) -> EmailVerified:
    """Consumed by the web app's sign-in; the single-use token is the only credential."""
    try:
        async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
            verified = await email_login.verify(session, body.token)
    except email_login.InvalidLoginLinkError as exc:
        raise NotFoundError(exc.message) from exc
    return EmailVerified(subject=verified.subject, email=verified.email)


def _rows(items: list[Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    def value(item: Any, name: str) -> Any:
        raw = getattr(item, name)
        if isinstance(raw, datetime):
            return raw.isoformat()
        if isinstance(raw, uuid.UUID):
            return str(raw)
        if isinstance(raw, Decimal):
            return float(raw)
        return raw

    return [{name: value(item, name) for name in fields} for item in items]


@router.get("/workspace/export")
async def export_workspace(account: WorkspaceOwner, session: Session) -> JSONResponse:
    """Everything this workspace owns, as JSON (data portability). Catalogue jobs are referenced."""
    current = account.require(WorkspaceRole.OWNER)

    async def all_of(model: Any) -> list[Any]:
        return list((await session.execute(select(model))).scalars().all())  # RLS: this workspace only

    workspace = await session.get(Workspace, current.id)
    members = (await session.execute(select(User.display_name, Membership.role).join(Membership))).all()
    followed = (
        await session.execute(
            select(Source.name, Source.kind, Source.locator, SourceSubscription.paused).join(SourceSubscription)
        )
    ).all()
    document = {
        "format": "jobpulse.workspace-export",
        "version": EXPORT_FORMAT_VERSION,
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "workspace": {
            "id": str(current.id),
            "name": workspace.name if workspace else current.name,
            "plan": current.plan,
        },
        "members": [{"name": name, "role": role} for name, role in members],
        "followed_boards": [
            {"name": name, "kind": kind, "locator": locator, "paused": paused}
            for name, kind, locator, paused in followed
        ],
        "profiles": _rows(
            await all_of(Profile),
            (
                "id",
                "display_name",
                "target_roles",
                "skills",
                "years_experience",
                "seniority",
                "home_country",
                "timezone",
                "summary",
                "policy",
                "notify_min_score",
                "notifications_enabled",
                "created_at",
            ),
        ),
        "job_states": _rows(
            await all_of(ProfileJob), ("profile_id", "job_id", "eligibility_status", "state", "match_score")
        ),
        "eligibility_decisions": _rows(
            await all_of(EligibilityDecision),
            ("id", "job_id", "profile_id", "stage", "status", "rules", "unresolved", "created_at"),
        ),
        "match_scores": _rows(
            await all_of(MatchScore),
            (
                "id",
                "job_id",
                "profile_id",
                "final_score",
                "actionable",
                "components",
                "matched_skills",
                "missing_skills",
                "created_at",
            ),
        ),
        "applications": _rows(
            await all_of(Application), ("id", "job_id", "profile_id", "status", "notes", "applied_at")
        ),
        "notifications": _rows(await all_of(Notification), ("id", "job_id", "channel", "status", "sent_at")),
        "audit_events": _rows(await all_of(AuditEvent), ("actor", "action", "entity_type", "entity_id", "created_at")),
    }
    filename = f"jobpulse-{current.slug}-{datetime.now(tz=UTC):%Y%m%d}.json"
    return JSONResponse(document, headers={"Content-Disposition": f'attachment; filename="{filename}"'})


async def _stop_polling_quietly(client: Client, settings: Settings, source_ids: list[str]) -> None:
    for source_id in source_ids:
        try:
            await temporal_service.stop_polling(client, source_id)
        except WorkflowServiceError as exc:
            logger.warning("workspace.delete_polling_stop_failed", source_id=source_id, error=exc.message)


@router.delete("/workspace", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    body: WorkspaceDelete,
    account: WorkspaceOwner,
    session: Session,
    ctx: Ctx,
    client: Temporal,
    background: BackgroundTasks,
) -> Response:
    """Permanently delete the workspace and all its data (catalogue jobs stay for others)."""
    current = account.require(WorkspaceRole.OWNER)
    if current.id == DEFAULT_WORKSPACE_ID:
        raise ConflictError("the public demo workspace cannot be deleted")
    workspace = await session.get(Workspace, current.id, with_for_update=True)
    if workspace is None:
        raise NotFoundError("workspace not found")
    if body.confirm_name != workspace.name:
        raise ConflictError("type the workspace name exactly to confirm deletion")
    if workspace.subscription_id and ctx.settings.billing_enabled:
        try:
            await cancel_subscription(session, ctx.settings, current.id)
        except BillingProviderError as exc:
            raise ConflictError("could not cancel the subscription; try again before deleting") from exc
    followed = [str(row[0]) for row in (await session.execute(select(SourceSubscription.source_id))).all()]
    async with system_section(session, account):
        await session.delete(workspace)  # cascades every tenant row
        await session.flush()
        orphaned: list[str] = []
        subscriptions = SubscriptionRepository(session)
        for source_id in followed:
            _, active = await subscriptions.follower_counts(uuid.UUID(source_id))
            source = await SourceRepository(session).get(uuid.UUID(source_id), for_update=True)
            if source is not None and active == 0 and source.enabled:
                source.enabled = False
                orphaned.append(source_id)
        await session.flush()
    logger.info("workspace.deleted", workspace_id=str(current.id), by=account.principal.actor)
    if orphaned:
        background.add_task(_stop_polling_quietly, client, ctx.settings, orphaned)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
