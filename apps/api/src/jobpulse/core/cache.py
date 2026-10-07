"""Short-TTL shared response cache (Redis) for hot, viewer-independent aggregates.

Fail-open: any Redis problem (or an open circuit) bypasses the cache and computes the value directly, so the
cache can never take the API down - it only sheds database load when healthy.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import structlog
from prometheus_client import Counter
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from jobpulse.core.circuit import CircuitBreaker

logger = structlog.get_logger(__name__)

CACHE_EVENTS = Counter("response_cache_total", "Response cache lookups", ["name", "result"])
KEY_PREFIX = "jp:cache:"


class ResponseCache:
    def __init__(self, redis: Redis | None, *, ttl_seconds: float, breaker: CircuitBreaker) -> None:
        self._redis = redis
        self._ttl_ms = int(ttl_seconds * 1000)
        self._breaker = breaker

    async def get_or_compute[M: BaseModel](self, name: str, model: type[M], compute: Callable[[], Awaitable[M]]) -> M:
        if self._redis is None or self._ttl_ms <= 0:
            return await compute()
        if not self._breaker.allow():
            CACHE_EVENTS.labels(name=name, result="bypass").inc()
            return await compute()
        key = f"{KEY_PREFIX}{name}"
        try:
            cached = await self._redis.get(key)
        except (RedisError, OSError) as exc:
            self._breaker.record_failure()
            CACHE_EVENTS.labels(name=name, result="error").inc()
            logger.warning("cache.unavailable", name=name, error=type(exc).__name__)
            return await compute()
        self._breaker.record_success()
        if cached is not None:
            CACHE_EVENTS.labels(name=name, result="hit").inc()
            return model.model_validate_json(cached)
        CACHE_EVENTS.labels(name=name, result="miss").inc()
        value = await compute()
        try:
            await self._redis.set(key, value.model_dump_json(), px=self._ttl_ms)
        except (RedisError, OSError) as exc:
            self._breaker.record_failure()
            logger.warning("cache.store_failed", name=name, error=type(exc).__name__)
        return value
