"""Billing: plan usage, checkout and verified-webhook processing (Razorpay)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.core.config import Settings
from jobpulse.core.errors import ConflictError, NotFoundError
from jobpulse.db.models import BillingEvent, Invitation, Membership, SourceSubscription, Workspace
from jobpulse.services.plans import GRACE_PERIOD, PAID_PLANS, PAID_STATES, Plan, effective_plan, limits_for
from jobpulse.services.razorpay import CheckoutSession, RazorpayClient, SubscriptionEvent
from jobpulse_core.errors import ValidationError

logger = structlog.get_logger(__name__)
PROVIDER = "razorpay"


@dataclass(frozen=True, slots=True)
class Usage:
    followed_sources: int
    members: int  # members + pending invitations


@dataclass(frozen=True, slots=True)
class WebhookOutcome:
    status: str  # "applied" | "duplicate" | "ignored"
    workspace_id: uuid.UUID | None = None


def workspace_plan(workspace: Workspace, now: datetime | None = None) -> Plan:
    return effective_plan(
        workspace.plan,
        subscription_status=workspace.subscription_status,
        grace_until=workspace.grace_until,
        now=now or datetime.now(tz=UTC),
    )


async def usage(session: AsyncSession, now: datetime | None = None) -> Usage:
    """Usage of the current workspace (RLS-scoped counts)."""
    moment = now or datetime.now(tz=UTC)
    sources = (await session.execute(select(func.count()).select_from(SourceSubscription))).scalar_one()
    members = (await session.execute(select(func.count()).select_from(Membership))).scalar_one()
    pending = (
        await session.execute(
            select(func.count())
            .select_from(Invitation)
            .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None), Invitation.expires_at > moment)
        )
    ).scalar_one()
    return Usage(followed_sources=int(sources), members=int(members) + int(pending))


def plan_ids(settings: Settings) -> dict[Plan, str]:
    configured = {Plan.PRO: settings.razorpay_plan_pro, Plan.TEAM: settings.razorpay_plan_team}
    return {plan: plan_id for plan, plan_id in configured.items() if plan_id}


async def start_checkout(
    session: AsyncSession, settings: Settings, workspace_id: uuid.UUID, plan: Plan
) -> CheckoutSession:
    """Owner action, workspace scope. The plan changes only when the webhook confirms payment."""
    if plan not in PAID_PLANS:
        raise ValidationError("choose a paid plan")
    plan_id = plan_ids(settings).get(plan)
    if plan_id is None:
        raise ValidationError(f"the {plan.value} plan is not available for purchase")
    workspace = await session.get(Workspace, workspace_id, with_for_update=True)
    if workspace is None:
        raise NotFoundError("workspace not found")
    if workspace.subscription_status in PAID_STATES and workspace_plan(workspace) is plan:
        raise ConflictError(f"this workspace is already on the {plan.value} plan")
    checkout = await RazorpayClient(settings).create_subscription(
        plan_id=plan_id, workspace_id=str(workspace_id), plan=plan.value
    )
    workspace.billing_provider = PROVIDER
    workspace.subscription_id = checkout.subscription_id
    workspace.subscription_status = checkout.status
    await session.flush()
    logger.info("billing.checkout_started", workspace_id=str(workspace_id), plan=plan.value)
    return checkout


async def cancel(session: AsyncSession, settings: Settings, workspace_id: uuid.UUID) -> str:
    """Cancel at the end of the paid cycle; the webhook records the final state."""
    workspace = await session.get(Workspace, workspace_id, with_for_update=True)
    if workspace is None or workspace.subscription_id is None:
        raise NotFoundError("no active subscription")
    return await RazorpayClient(settings).cancel_subscription(workspace.subscription_id, at_cycle_end=True)


async def apply_webhook(
    session: AsyncSession, settings: Settings, event: SubscriptionEvent, now: datetime | None = None
) -> WebhookOutcome:
    """System scope. Idempotent per provider event id; never trusts plan names from notes."""
    moment = now or datetime.now(tz=UTC)
    recorded = await session.execute(
        insert(BillingEvent)
        .values(provider=PROVIDER, event_id=event.event_id, event_type=event.event_type, payload=event.payload)
        .on_conflict_do_nothing()
        .returning(BillingEvent.event_id)
    )
    if recorded.scalar_one_or_none() is None:
        return WebhookOutcome(status="duplicate")

    by_subscription = select(Workspace).where(Workspace.subscription_id == event.subscription_id).with_for_update()
    workspace = (await session.execute(by_subscription)).scalar_one_or_none()
    if workspace is None and event.workspace_id is not None:
        # A checkout the owner abandoned and then completed anyway: our own notes name the workspace.
        workspace = await session.get(Workspace, uuid.UUID(event.workspace_id), with_for_update=True)
    if workspace is None:
        logger.warning("billing.webhook_unmatched", event_type=event.event_type, subscription=event.subscription_id)
        return WebhookOutcome(status="ignored")

    plan_by_id = {plan_id: plan for plan, plan_id in plan_ids(settings).items()}
    paid_plan = plan_by_id.get(event.plan_id or "")
    workspace.billing_provider = PROVIDER
    workspace.subscription_id = event.subscription_id
    workspace.subscription_status = event.status
    if event.current_end is not None:
        workspace.current_period_end = event.current_end
    if event.status in PAID_STATES:
        if paid_plan is not None:
            workspace.plan = paid_plan.value
        workspace.grace_until = None
    else:
        # halted / cancelled / completed / expired / paused: keep paid limits briefly.
        period_end = workspace.current_period_end or moment
        workspace.grace_until = max(period_end, moment) + GRACE_PERIOD
    await session.execute(
        update(BillingEvent)
        .where(BillingEvent.provider == PROVIDER, BillingEvent.event_id == event.event_id)
        .values(workspace_id=workspace.id)
    )
    await session.flush()
    logger.info(
        "billing.webhook_applied",
        workspace_id=str(workspace.id),
        event_type=event.event_type,
        status=event.status,
        plan=workspace.plan,
    )
    return WebhookOutcome(status="applied", workspace_id=workspace.id)


def limits_summary(plan: Plan) -> dict[str, int]:
    limits = limits_for(plan)
    return {
        "followed_sources": limits.followed_sources,
        "min_poll_interval_seconds": limits.min_poll_interval_seconds,
        "members": limits.members,
        "reevaluations_per_day": limits.reevaluations_per_day,
    }
