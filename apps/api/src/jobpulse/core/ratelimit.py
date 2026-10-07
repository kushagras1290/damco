"""Sliding-window rate limiting: Redis (shared across instances) with an in-memory fallback.

The Redis limiter keeps an exact sliding-window log per key in a sorted set, updated by
one atomic Lua script that uses the Redis server clock, so instances with skewed clocks
still agree. If Redis is unreachable the limiter fails *open to the local limiter* -
traffic is still bounded per instance and the outage is surfaced as a metric/log, but
the API keeps serving.
"""

from __future__ import annotations

import math
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Protocol

import structlog
from prometheus_client import Counter
from redis.asyncio import Redis
from redis.exceptions import RedisError

from jobpulse.core.circuit import CircuitBreaker

logger = structlog.get_logger(__name__)

RATE_LIMIT_FALLBACKS = Counter(
    "rate_limit_backend_fallbacks_total", "Rate-limit decisions served by the local fallback (Redis unavailable)"
)
KEY_PREFIX = "jp:rl:"
MAX_LOCAL_KEYS = 50_000

# KEYS[1]=bucket  ARGV[1]=window_ms  ARGV[2]=limit  ARGV[3]=unique member
# Returns {allowed(0/1), remaining, reset_ms}
SLIDING_WINDOW_LUA = """
local now_parts = redis.call('TIME')
local now = tonumber(now_parts[1]) * 1000 + math.floor(tonumber(now_parts[2]) / 1000)
local window = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - window)
local count = redis.call('ZCARD', KEYS[1])
if count < limit then
  redis.call('ZADD', KEYS[1], now, ARGV[3])
  redis.call('PEXPIRE', KEYS[1], window)
  return {1, limit - count - 1, window}
end
local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
local reset = window
if oldest[2] then reset = tonumber(oldest[2]) + window - now end
return {0, 0, reset}
"""


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateDecision: ...


class MemoryRateLimiter:
    """Exact sliding-window log in process memory (bounded number of keys, LRU)."""

    def __init__(self, max_keys: int = MAX_LOCAL_KEYS) -> None:
        self._max_keys = max_keys
        self._logs: OrderedDict[str, deque[float]] = OrderedDict()

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateDecision:
        now = time.monotonic()
        log = self._logs.get(key)
        if log is None:
            log = deque()
            self._logs[key] = log
            if len(self._logs) > self._max_keys:
                self._logs.popitem(last=False)
        else:
            self._logs.move_to_end(key)
        while log and log[0] <= now - window_seconds:
            log.popleft()
        if len(log) < limit:
            log.append(now)
            return RateDecision(True, limit, limit - len(log), window_seconds)
        reset = max(1, math.ceil(log[0] + window_seconds - now))
        return RateDecision(False, limit, 0, reset)


class RedisRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis
        self._script = redis.register_script(SLIDING_WINDOW_LUA)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateDecision:
        allowed, remaining, reset_ms = await self._script(
            keys=[f"{KEY_PREFIX}{key}"],
            args=[window_seconds * 1000, limit, uuid.uuid4().hex],
        )
        return RateDecision(bool(allowed), limit, int(remaining), max(1, math.ceil(int(reset_ms) / 1000)))


class ResilientRateLimiter:
    """Redis first; on Redis errors (or while its circuit is open) use the per-instance limiter."""

    def __init__(self, primary: RateLimiter | None, fallback: RateLimiter, *, breaker: CircuitBreaker) -> None:
        self._primary = primary
        self._fallback = fallback
        self._breaker = breaker

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateDecision:
        if self._primary is not None:
            if self._breaker.allow():
                try:
                    decision = await self._primary.hit(key, limit=limit, window_seconds=window_seconds)
                except (RedisError, OSError) as exc:
                    self._breaker.record_failure()
                    logger.warning("ratelimit.redis_unavailable", error=type(exc).__name__)
                else:
                    self._breaker.record_success()
                    return decision
            RATE_LIMIT_FALLBACKS.inc()
        return await self._fallback.hit(key, limit=limit, window_seconds=window_seconds)
