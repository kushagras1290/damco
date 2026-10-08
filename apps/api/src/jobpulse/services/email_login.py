"""Passwordless email sign-in (magic links) without a session database.

Flow: the browser posts an address to ``/auth/email/start``; we store only a SHA-256 of a
random single-use token (15 minutes) and email a link to the web app. The web app's
Auth.js Credentials provider exchanges the token at ``/auth/email/verify`` and signs the
user in as ``email:<sha256(normalized address)>`` - an immutable identity subject.

Never reveals whether an address has an account (``start`` always answers the same).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.core.config import Settings
from jobpulse.core.errors import RateLimitedError, ServiceUnavailableError
from jobpulse.db.models import EmailLoginToken
from jobpulse_core.errors import JobPulseError

logger = structlog.get_logger(__name__)

TOKEN_TTL = timedelta(minutes=15)
TOKEN_BYTES = 32
MAX_LINKS_PER_EMAIL_PER_HOUR = 5
RATE_KEY_PREFIX = "jp:email-login:"
RATE_WINDOW_SECONDS = 3600
RESEND_URL = "https://api.resend.com/emails"
RESEND_TIMEOUT_SECONDS = 10.0


class InvalidLoginLinkError(JobPulseError):
    """Unknown, expired or already used sign-in link."""


@dataclass(frozen=True, slots=True)
class VerifiedEmail:
    subject: str  # sha256 hex of the normalized address
    email: str


def normalize_email(address: str) -> str:
    return address.strip().lower()


def email_subject(address: str) -> str:
    return hashlib.sha256(normalize_email(address).encode("utf-8")).hexdigest()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def delivery_available(settings: Settings) -> bool:
    return settings.resend_api_key is not None and bool(settings.notification_from_email)


async def _throttle(redis: Redis | None, subject: str) -> None:
    """Per-address cap (on top of per-IP limits) so nobody can mail-bomb an inbox."""
    if redis is None:
        return
    key = f"{RATE_KEY_PREFIX}{subject}"
    try:
        sent = int(await redis.incr(key))
        if sent == 1:
            await redis.expire(key, RATE_WINDOW_SECONDS)
    except (RedisError, OSError) as exc:
        logger.warning("email_login.throttle_unavailable", error=type(exc).__name__)
        return
    if sent > MAX_LINKS_PER_EMAIL_PER_HOUR:
        raise RateLimitedError("too many sign-in links requested for this address; try again later")


async def _send(settings: Settings, address: str, link: str) -> None:
    if settings.resend_api_key is None or not settings.notification_from_email:
        raise ServiceUnavailableError("email sign-in is not configured")
    body = {
        "from": settings.notification_from_email,
        "to": [address],
        "subject": "Your JobPulse sign-in link",
        "text": (
            f"Sign in to JobPulse:\n\n{link}\n\n"
            "The link works once and expires in 15 minutes. If you did not request it, ignore this email."
        ),
    }
    headers = {"Authorization": f"Bearer {settings.resend_api_key.get_secret_value()}"}
    try:
        async with httpx.AsyncClient(timeout=RESEND_TIMEOUT_SECONDS) as http:
            response = await http.post(RESEND_URL, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise ServiceUnavailableError("could not send the sign-in email, try again") from exc
    if response.status_code >= 400:
        logger.warning("email_login.send_failed", status=response.status_code)
        raise ServiceUnavailableError("could not send the sign-in email, try again")


async def start(session: AsyncSession, settings: Settings, redis: Redis | None, address: str) -> None:
    """System scope. Same response whether or not the address already has an account."""
    email = normalize_email(address)
    subject = email_subject(email)
    await _throttle(redis, subject)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    await session.execute(
        insert(EmailLoginToken).values(
            token_hash=_hash_token(token), email=email, expires_at=datetime.now(tz=UTC) + TOKEN_TTL
        )
    )
    link = f"{settings.dashboard_base_url.rstrip('/')}/auth/email?token={token}"
    if delivery_available(settings):
        await _send(settings, email, link)
    elif settings.environment in {"local", "test"}:
        # Development convenience only: production refuses to start the flow without delivery.
        logger.info("email_login.dev_link", link=link)
    else:
        raise ServiceUnavailableError("email sign-in is not configured")
    logger.info("email_login.link_sent", subject=subject[:12])


async def verify(session: AsyncSession, token: str) -> VerifiedEmail:
    """System scope. Single use: the token is consumed atomically."""
    now = datetime.now(tz=UTC)
    consumed = await session.execute(
        update(EmailLoginToken)
        .where(
            EmailLoginToken.token_hash == _hash_token(token),
            EmailLoginToken.used_at.is_(None),
            EmailLoginToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(EmailLoginToken.email)
    )
    email = consumed.scalar_one_or_none()
    if email is None:
        raise InvalidLoginLinkError("this sign-in link is invalid, expired or already used")
    return VerifiedEmail(subject=email_subject(email), email=email)
