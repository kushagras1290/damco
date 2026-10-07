"""Notification provider contract and the two initial providers."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import time
from dataclasses import asdict, dataclass, field
from typing import Protocol, runtime_checkable

from jobpulse_core.errors import (
    ConfigurationError,
    NotificationDeliveryError,
    NotificationRejectedError,
    SourceFetchError,
    SourceNotFoundError,
    SourceRateLimitedError,
    UnsafeUrlError,
)
from jobpulse_core.ingestion.http import SafeHttpClient

RESEND_ENDPOINT = "https://api.resend.com/emails"
SIGNATURE_HEADER = "X-JobPulse-Signature"
TIMESTAMP_HEADER = "X-JobPulse-Timestamp"
MAX_REASONS = 8


@dataclass(frozen=True, slots=True)
class NotificationMessage:
    """Provider-agnostic payload. ``dedupe_key`` doubles as the idempotency key."""

    dedupe_key: str
    job_id: str
    title: str
    company: str
    url: str
    location: str | None
    score: float
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    dashboard_url: str | None = None


@runtime_checkable
class NotificationProvider(Protocol):
    channel: str

    async def send(self, notification: NotificationMessage) -> None: ...


def render_email_html(message: NotificationMessage) -> str:
    esc = html.escape
    location = esc(message.location or "Location n/a")
    reasons = "".join(f"<li>{esc(reason)}</li>" for reason in message.reasons[:MAX_REASONS])
    matched = esc(", ".join(message.matched_skills)) or "-"
    missing = esc(", ".join(message.missing_skills)) or "-"
    dashboard = (
        f'<p><a href="{esc(message.dashboard_url, quote=True)}">Open decision trace in JobPulse</a></p>'
        if message.dashboard_url
        else ""
    )
    return (
        '<div style="font-family:system-ui,sans-serif;max-width:560px">'
        f'<h2 style="margin:0 0 4px">{esc(message.title)}</h2>'
        f'<p style="margin:0 0 12px;color:#555">{esc(message.company)} &middot; {location}</p>'
        f"<p><strong>Match score:</strong> {message.score:.0%}</p>"
        f"<p><strong>Matched skills:</strong> {matched}<br><strong>Missing skills:</strong> {missing}</p>"
        f"<ul>{reasons}</ul>"
        f'<p><a href="{esc(message.url, quote=True)}">View posting</a></p>{dashboard}</div>'
    )


def sign_webhook_payload(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode() + body, hashlib.sha256)
    return f"sha256={mac.hexdigest()}"


def _translate(exc: Exception, channel: str) -> Exception:
    if isinstance(exc, (SourceRateLimitedError, SourceFetchError)):
        return NotificationDeliveryError(f"{channel} delivery failed: {exc}", context={"channel": channel})
    if isinstance(exc, (SourceNotFoundError, UnsafeUrlError)):
        return NotificationRejectedError(f"{channel} rejected: {exc}", context={"channel": channel})
    return exc


class ResendEmailProvider:
    channel = "email"

    def __init__(self, *, api_key: str, sender: str, recipient: str, http: SafeHttpClient) -> None:
        if not api_key or not sender or not recipient:
            raise ConfigurationError("Resend requires api_key, sender and recipient")
        self._api_key = api_key
        self._sender = sender
        self._recipient = recipient
        self._http = http

    async def send(self, notification: NotificationMessage) -> None:
        body = {
            "from": self._sender,
            "to": [self._recipient],
            "subject": f"[JobPulse {notification.score:.0%}] {notification.title} @ {notification.company}",
            "html": render_email_html(notification),
        }
        try:
            await self._http.fetch(
                RESEND_ENDPOINT,
                method="POST",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": notification.dedupe_key[:256],
                },
                json_body=body,
            )
        except (SourceFetchError, SourceNotFoundError, UnsafeUrlError) as exc:
            raise _translate(exc, self.channel) from exc


class WebhookProvider:
    """POSTs a JSON payload signed with HMAC-SHA256 over ``"{timestamp}.{body}"``."""

    channel = "webhook"

    def __init__(self, *, url: str, secret: str, http: SafeHttpClient) -> None:
        if not url or not secret:
            raise ConfigurationError("webhook requires url and signing secret")
        self._url = url
        self._secret = secret
        self._http = http

    async def send(self, notification: NotificationMessage) -> None:
        payload = {"type": "job.matched", "data": asdict(notification)}
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        timestamp = str(int(time.time()))
        try:
            await self._http.fetch(
                self._url,
                method="POST",
                headers={
                    "Content-Type": "application/json",
                    SIGNATURE_HEADER: sign_webhook_payload(self._secret, timestamp, body),
                    TIMESTAMP_HEADER: timestamp,
                    "Idempotency-Key": notification.dedupe_key,
                },
                content=body,
            )
        except (SourceFetchError, SourceNotFoundError, UnsafeUrlError) as exc:
            raise _translate(exc, self.channel) from exc
