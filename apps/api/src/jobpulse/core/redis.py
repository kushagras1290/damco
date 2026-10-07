"""Redis client factory: strict timeouts, bounded retries, health checks."""

from __future__ import annotations

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from jobpulse.core.config import Settings
from jobpulse_core.errors import JobPulseError

RETRY_ATTEMPTS = 2
HEALTH_CHECK_INTERVAL_SECONDS = 30
MAX_CONNECTIONS = 50


class RedisConfigError(JobPulseError):
    """The API cannot run safely with the configured Redis settings."""


def require_redis_for_api(settings: Settings) -> None:
    """API-only rule (the worker never touches Redis, so it is not a shared-settings rule).

    Multiple production API instances need shared rate-limit windows and idempotency keys;
    per-instance memory would silently multiply limits and allow duplicate writes.
    """
    if settings.environment == "production" and settings.redis_url is None:
        msg = "REDIS_URL is required for the API in production (shared rate limits, cache, idempotency)"
        raise RedisConfigError(msg)


def build_redis(settings: Settings, *, client_name: str) -> Redis | None:
    """A pooled client, or None when REDIS_URL is unset (local dev without Redis)."""
    if settings.redis_url is None:
        return None
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_timeout=settings.redis_socket_timeout_seconds,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        retry=Retry(ExponentialBackoff(cap=0.2, base=0.02), RETRY_ATTEMPTS),
        retry_on_error=[RedisConnectionError, RedisTimeoutError],
        health_check_interval=HEALTH_CHECK_INTERVAL_SECONDS,
        max_connections=MAX_CONNECTIONS,
        client_name=client_name,
        decode_responses=False,
    )
