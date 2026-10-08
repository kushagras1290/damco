"""/api/v1/billing (plans, usage, checkout) and /api/v1/webhooks/razorpay."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from jobpulse.api.deps import Ctx
from jobpulse.api.tenancy import CurrentAccount, Member, Session, WorkspaceOwner, system_section
from jobpulse.core.errors import AuthenticationError, NotFoundError, PermissionDeniedError
from jobpulse.db.models import Workspace
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE
from jobpulse.repositories.activity import AuditRepository
from jobpulse.services import billing
from jobpulse.services.accounts import WorkspaceRole
from jobpulse.services.plans import Plan
from jobpulse.services.razorpay import WebhookVerificationError, parse_subscription_event, verify_signature

router = APIRouter(prefix="/api/v1", tags=["billing"])

PaidPlan = Literal["pro", "team"]


class BillingOut(BaseModel):
    plan: str
    billing_enabled: bool
    purchasable: list[str]
    limits: dict[str, int]
    usage: dict[str, int]
    subscription_status: str | None
    current_period_end: str | None
    grace_until: str | None


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: PaidPlan


class CheckoutOut(BaseModel):
    checkout_url: str
    subscription_id: str


class PlanOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: Literal["free", "pro", "team"]


@router.get("/billing", response_model=BillingOut)
async def get_billing(account: Member, session: Session, ctx: Ctx) -> BillingOut:
    current = account.require(WorkspaceRole.MEMBER)
    workspace = await session.get(Workspace, current.id)
    if workspace is None:
        raise NotFoundError("workspace not found")
    plan = billing.workspace_plan(workspace)
    used = await billing.usage(session)
    return BillingOut(
        plan=plan.value,
        billing_enabled=ctx.settings.billing_enabled,
        purchasable=[p.value for p in billing.plan_ids(ctx.settings)],
        limits=billing.limits_summary(plan),
        usage={"followed_sources": used.followed_sources, "members": used.members},
        subscription_status=workspace.subscription_status,
        current_period_end=workspace.current_period_end.isoformat() if workspace.current_period_end else None,
        grace_until=workspace.grace_until.isoformat() if workspace.grace_until else None,
    )


@router.post("/billing/checkout", response_model=CheckoutOut)
async def checkout(body: CheckoutRequest, account: WorkspaceOwner, session: Session, ctx: Ctx) -> CheckoutOut:
    if not ctx.settings.billing_enabled:
        raise NotFoundError("billing is not enabled on this deployment")
    current = account.require(WorkspaceRole.OWNER)
    session_ = await billing.start_checkout(session, ctx.settings, current.id, Plan(body.plan))
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="billing.checkout",
        entity_type="workspace",
        entity_id=str(current.id),
        payload={"plan": body.plan},
    )
    return CheckoutOut(checkout_url=session_.checkout_url, subscription_id=session_.subscription_id)


@router.post("/billing/cancel")
async def cancel(account: WorkspaceOwner, session: Session, ctx: Ctx) -> dict[str, str]:
    if not ctx.settings.billing_enabled:
        raise NotFoundError("billing is not enabled on this deployment")
    current = account.require(WorkspaceRole.OWNER)
    status = await billing.cancel(session, ctx.settings, current.id)
    await AuditRepository(session).record(
        actor=account.principal.actor, action="billing.cancel", entity_type="workspace", entity_id=str(current.id)
    )
    return {"status": status}


@router.patch("/admin/workspaces/{workspace_id}/plan")
async def override_plan(
    workspace_id: uuid.UUID, body: PlanOverride, account: CurrentAccount, session: Session
) -> dict[str, str]:
    """Platform admins set plans for sales-led deals and trials (no provider subscription)."""
    if not account.principal.platform_admin:
        raise PermissionDeniedError("platform admin only")
    async with system_section(session, account):
        workspace = await session.get(Workspace, workspace_id, with_for_update=True)
        if workspace is None:
            raise NotFoundError("workspace not found")
        workspace.plan = body.plan
        workspace.grace_until = None
        await AuditRepository(session).record(
            actor=account.principal.actor,
            action="billing.plan_override",
            entity_type="workspace",
            entity_id=str(workspace_id),
            payload={"plan": body.plan},
            workspace_id=workspace_id,
        )
    return {"plan": body.plan}


@router.post("/webhooks/razorpay", include_in_schema=False)
async def razorpay_webhook(request: Request, ctx: Ctx) -> dict[str, str]:
    """Public endpoint: authenticated solely by the HMAC signature over the raw body."""
    secret = ctx.settings.razorpay_webhook_secret
    if secret is None:
        raise NotFoundError("billing is not enabled on this deployment")
    raw = await request.body()
    try:
        verify_signature(raw, request.headers.get("x-razorpay-signature"), secret.get_secret_value())
        event = parse_subscription_event(raw, request.headers.get("x-razorpay-event-id"))
    except (WebhookVerificationError, ValueError) as exc:
        raise AuthenticationError("invalid webhook") from exc
    if event is None:
        return {"status": "ignored"}
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        outcome = await billing.apply_webhook(session, ctx.settings, event)
    return {"status": outcome.status}
