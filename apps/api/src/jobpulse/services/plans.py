"""Plans, their limits, and the effective plan of a workspace (billing-state aware)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from jobpulse.core.errors import ApiError


class Plan(StrEnum):
    FREE = "free"
    PRO = "pro"
    TEAM = "team"


@dataclass(frozen=True, slots=True)
class PlanLimits:
    followed_sources: int
    min_poll_interval_seconds: int
    members: int  # members + pending invitations
    reevaluations_per_day: int


LIMITS: dict[Plan, PlanLimits] = {
    Plan.FREE: PlanLimits(followed_sources=5, min_poll_interval_seconds=900, members=1, reevaluations_per_day=20),
    Plan.PRO: PlanLimits(followed_sources=50, min_poll_interval_seconds=120, members=1, reevaluations_per_day=200),
    Plan.TEAM: PlanLimits(followed_sources=500, min_poll_interval_seconds=60, members=25, reevaluations_per_day=2000),
}
PAID_PLANS = frozenset({Plan.PRO, Plan.TEAM})
GRACE_PERIOD = timedelta(days=3)
QUOTA_KEY_PREFIX = "jp:quota:"
QUOTA_KEY_TTL_SECONDS = 2 * 24 * 60 * 60
logger = structlog.get_logger(__name__)
# Provider subscription states that keep the paid plan (pending = a charge failed, retries running).
PAID_STATES = frozenset({"authenticated", "active", "pending", "resumed"})


class PlanLimitError(ApiError):
    status_code = 402
    code = "plan_limit"
    public_context = True


def effective_plan(
    plan: str,
    *,
    subscription_status: str | None,
    grace_until: datetime | None,
    now: datetime,
) -> Plan:
    """The plan a workspace gets right now.

    No subscription (free, or set by a platform admin) -> the stored plan. A paid subscription
    keeps its plan while active/pending, and for ``GRACE_PERIOD`` after it lapses.
    """
    stored = Plan(plan)
    if subscription_status is None or stored not in PAID_PLANS:
        return stored
    if subscription_status in PAID_STATES:
        return stored
    if grace_until is not None and now < grace_until:
        return stored
    return Plan.FREE


def limits_for(plan: str | Plan) -> PlanLimits:
    return LIMITS[Plan(plan)]


async def consume_daily_quota(
    redis: Redis | None, *, name: str, workspace_id: uuid.UUID, plan: str | Plan, limit: int
) -> None:
    """Count one use against a per-workspace UTC-day quota (Redis). Fails open without Redis:
    production requires Redis, and a quota outage must not block paying customers."""
    if redis is None:
        return
    key = f"{QUOTA_KEY_PREFIX}{name}:{workspace_id}:{datetime.now(tz=UTC):%Y%m%d}"
    try:
        used = int(await redis.incr(key))
        if used == 1:
            await redis.expire(key, QUOTA_KEY_TTL_SECONDS)
    except (RedisError, OSError) as exc:
        logger.warning("quota.unavailable", name=name, error=type(exc).__name__)
        return
    if used > limit:
        raise PlanLimitError(
            f"the {Plan(plan).value} plan allows {limit} {name} per day; try tomorrow or upgrade",
            context={"plan": Plan(plan).value, "limit": limit, "used": limit},
        )


def require_capacity(plan: str | Plan, *, what: str, used: int, limit: int) -> None:
    if used >= limit:
        raise PlanLimitError(
            f"the {Plan(plan).value} plan allows {limit} {what}; upgrade to add more",
            context={"plan": Plan(plan).value, "limit": limit, "used": used},
        )
