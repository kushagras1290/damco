"""Razorpay Subscriptions client and webhook verification.

API: https://razorpay.com/docs/api/payments/subscriptions/ (Basic auth key_id:key_secret).
Webhooks: HMAC-SHA256 of the *raw* request body with the webhook secret, hex-encoded in
``X-Razorpay-Signature``; ``x-razorpay-event-id`` is unique per event (idempotency).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from jobpulse.core.config import Settings
from jobpulse_core.errors import JobPulseError

logger = structlog.get_logger(__name__)

SUBSCRIPTION_EVENT_PREFIX = "subscription."
MAX_ERROR_BODY_CHARS = 300


class BillingProviderError(JobPulseError):
    """Razorpay rejected the request or is unavailable."""

    retryable = True


class BillingProviderRejectedError(BillingProviderError):
    """Razorpay answered 4xx: retrying the same request will not help."""

    retryable = False


class WebhookVerificationError(JobPulseError):
    """Signature missing or invalid: the request did not come from Razorpay."""


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    subscription_id: str
    status: str
    checkout_url: str


@dataclass(frozen=True, slots=True)
class SubscriptionEvent:
    event_id: str
    event_type: str  # e.g. "subscription.activated"
    subscription_id: str
    status: str
    plan_id: str | None
    current_end: datetime | None
    workspace_id: str | None  # from the notes we set at checkout
    payload: dict[str, Any]


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    if not signature:
        raise WebhookVerificationError("missing webhook signature")
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise WebhookVerificationError("invalid webhook signature")


def parse_subscription_event(raw_body: bytes, event_id: str | None) -> SubscriptionEvent | None:
    """Subscription lifecycle events only; anything else returns None (acknowledged, ignored)."""
    if not event_id:
        raise WebhookVerificationError("missing webhook event id")
    payload = json.loads(raw_body)
    event_type = str(payload.get("event", ""))
    if not event_type.startswith(SUBSCRIPTION_EVENT_PREFIX):
        return None
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    if not isinstance(entity, dict) or not entity.get("id"):
        return None
    raw_notes = entity.get("notes")
    notes: dict[str, Any] = raw_notes if isinstance(raw_notes, dict) else {}
    current_end = entity.get("current_end")
    return SubscriptionEvent(
        event_id=event_id,
        event_type=event_type,
        subscription_id=str(entity["id"]),
        status=str(entity.get("status", "")),
        plan_id=str(entity["plan_id"]) if entity.get("plan_id") else None,
        current_end=datetime.fromtimestamp(int(current_end), tz=UTC) if current_end else None,
        workspace_id=str(notes["workspace_id"]) if notes.get("workspace_id") else None,
        payload=payload,
    )


class RazorpayClient:
    def __init__(self, settings: Settings) -> None:
        if settings.razorpay_key_id is None or settings.razorpay_key_secret is None:
            msg = "Razorpay is not configured"
            raise BillingProviderError(msg)
        self._settings = settings
        self._auth = (settings.razorpay_key_id, settings.razorpay_key_secret.get_secret_value())

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._settings.razorpay_api_base.rstrip('/')}{path}"
        try:
            async with httpx.AsyncClient(timeout=self._settings.razorpay_timeout_seconds) as http:
                response = await http.post(url, json=body, auth=self._auth)
        except httpx.HTTPError as exc:
            raise BillingProviderError("payment provider unavailable", context={"error": type(exc).__name__}) from exc
        if response.status_code >= 400:
            logger.warning("razorpay.error", status=response.status_code, path=path)
            context = {"status": response.status_code, "body": response.text[:MAX_ERROR_BODY_CHARS]}
            if response.status_code >= 500:
                raise BillingProviderError("payment provider unavailable", context=context)
            raise BillingProviderRejectedError("payment provider rejected the request", context=context)
        data: dict[str, Any] = response.json()
        return data

    async def create_subscription(self, *, plan_id: str, workspace_id: str, plan: str) -> CheckoutSession:
        data = await self._post(
            "/subscriptions",
            {
                "plan_id": plan_id,
                "total_count": self._settings.razorpay_total_count,
                "customer_notify": True,
                "notes": {"workspace_id": workspace_id, "plan": plan},
            },
        )
        return CheckoutSession(
            subscription_id=str(data["id"]), status=str(data["status"]), checkout_url=str(data["short_url"])
        )

    async def cancel_subscription(self, subscription_id: str, *, at_cycle_end: bool = True) -> str:
        data = await self._post(f"/subscriptions/{subscription_id}/cancel", {"cancel_at_cycle_end": at_cycle_end})
        return str(data.get("status", "cancelled"))
