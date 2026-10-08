"""Plan resolution and Razorpay webhook verification (pure logic)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

import pytest

from jobpulse.services.plans import Plan, effective_plan, limits_for
from jobpulse.services.razorpay import WebhookVerificationError, parse_subscription_event, verify_signature

NOW = datetime(2026, 10, 8, tzinfo=UTC)
SECRET = "whsec_test"


@pytest.mark.parametrize(
    ("plan", "status", "grace", "expected"),
    [
        ("free", None, None, Plan.FREE),
        ("team", None, None, Plan.TEAM),  # set by a platform admin, no subscription
        ("pro", "active", None, Plan.PRO),
        ("pro", "pending", None, Plan.PRO),  # charge failed, retries running
        ("pro", "halted", NOW + timedelta(days=1), Plan.PRO),  # inside grace
        ("pro", "halted", NOW - timedelta(seconds=1), Plan.FREE),  # grace over
        ("pro", "cancelled", None, Plan.FREE),
        ("pro", "created", None, Plan.FREE),  # checkout started, never paid
    ],
)
def test_effective_plan(plan: str, status: str | None, grace: datetime | None, expected: Plan) -> None:
    assert effective_plan(plan, subscription_status=status, grace_until=grace, now=NOW) is expected


def test_plans_are_strictly_more_generous() -> None:
    free, pro, team = (limits_for(p) for p in (Plan.FREE, Plan.PRO, Plan.TEAM))
    assert free.followed_sources < pro.followed_sources < team.followed_sources
    assert free.min_poll_interval_seconds > pro.min_poll_interval_seconds >= team.min_poll_interval_seconds
    assert team.members > 1


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_verification() -> None:
    body = b'{"event":"subscription.activated"}'
    verify_signature(body, sign(body), SECRET)
    with pytest.raises(WebhookVerificationError):
        verify_signature(body, sign(body, "other"), SECRET)
    with pytest.raises(WebhookVerificationError):
        verify_signature(body + b" ", sign(body), SECRET)  # any byte change breaks it
    with pytest.raises(WebhookVerificationError):
        verify_signature(body, None, SECRET)


def test_parse_subscription_event() -> None:
    payload = {
        "event": "subscription.charged",
        "payload": {
            "subscription": {
                "entity": {
                    "id": "sub_1",
                    "plan_id": "plan_pro",
                    "status": "active",
                    "current_end": 1_790_000_000,
                    "notes": {"workspace_id": "ws-1", "plan": "team"},
                }
            }
        },
    }
    event = parse_subscription_event(json.dumps(payload).encode(), "evt_1")
    assert event is not None
    assert (event.subscription_id, event.status, event.plan_id, event.workspace_id) == (
        "sub_1",
        "active",
        "plan_pro",
        "ws-1",
    )
    assert event.current_end == datetime.fromtimestamp(1_790_000_000, tz=UTC)
    assert parse_subscription_event(b'{"event":"payment.captured","payload":{}}', "evt_2") is None
    with pytest.raises(WebhookVerificationError):
        parse_subscription_event(json.dumps(payload).encode(), None)  # no event id -> no idempotency
