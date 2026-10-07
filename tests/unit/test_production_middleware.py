"""Idempotency, request-timeout and limiter edge cases with in-process fakes (no containers)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from starlette.types import ASGIApp, Scope

from jobpulse.core.circuit import CircuitBreaker, CircuitState
from jobpulse.core.idempotency import IdempotencyMiddleware
from jobpulse.core.middleware import RequestTimeoutMiddleware
from jobpulse.core.ratelimit import MemoryRateLimiter, RateDecision, ResilientRateLimiter

KEY = {"Idempotency-Key": "test-key-0001"}


class FakeRedis:
    """Just enough of redis.asyncio.Redis for the idempotency middleware."""

    def __init__(self, *, fail: bool = False) -> None:
        self.data: dict[str, bytes] = {}
        self.fail = fail

    def _check(self) -> None:
        if self.fail:
            raise RedisConnectionError("down")

    async def get(self, key: str) -> bytes | None:
        self._check()
        return self.data.get(key)

    async def set(self, key: str, value: str | bytes, *, nx: bool = False, ex: int | None = None) -> bool | None:
        self._check()
        if nx and key in self.data:
            return None
        self.data[key] = value.encode() if isinstance(value, str) else value
        return True

    async def delete(self, key: str) -> int:
        self._check()
        return int(self.data.pop(key, None) is not None)


class Counter:
    def __init__(self) -> None:
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()


def build_app(counter: Counter, *, status: int = 201) -> Starlette:
    async def create(request: Request) -> Response:
        counter.calls += 1
        counter.entered.set()
        await counter.release.wait()
        body = await request.json()
        return JSONResponse({"call": counter.calls, "echo": body}, status_code=status)

    return Starlette(routes=[Route("/things", create, methods=["POST"])])


def wrap(app: ASGIApp, redis: FakeRedis | None) -> ASGIApp:
    def caller_key(scope: Scope) -> str:
        return "caller"

    return IdempotencyMiddleware(
        app,
        redis=redis,  # type: ignore[arg-type]
        ttl_seconds=60,
        caller_key=caller_key,
        breaker=CircuitBreaker("test-idem"),
    )


def client(app: ASGIApp) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_replay_executes_once() -> None:
    counter = Counter()
    async with client(wrap(build_app(counter), FakeRedis())) as http:
        first = await http.post("/things", json={"a": 1}, headers=KEY)
        second = await http.post("/things", json={"a": 1}, headers=KEY)
    assert counter.calls == 1
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotent-replayed"] == "true"


async def test_concurrent_duplicate_gets_409() -> None:
    counter = Counter()
    counter.release.clear()
    async with client(wrap(build_app(counter), FakeRedis())) as http:
        in_flight = asyncio.create_task(http.post("/things", json={"a": 1}, headers=KEY))
        await asyncio.wait_for(counter.entered.wait(), timeout=5)
        duplicate = await http.post("/things", json={"a": 1}, headers=KEY)
        counter.release.set()
        original = await in_flight
    assert duplicate.status_code == 409
    assert duplicate.headers["retry-after"] == "1"
    assert original.status_code == 201
    assert counter.calls == 1


async def test_server_errors_are_not_remembered() -> None:
    counter = Counter()
    redis = FakeRedis()
    async with client(wrap(build_app(counter, status=503), redis)) as http:
        await http.post("/things", json={"a": 1}, headers=KEY)
        retry = await http.post("/things", json={"a": 1}, headers=KEY)
    assert counter.calls == 2  # a retry after a 5xx really executes again
    assert "idempotent-replayed" not in retry.headers
    assert not any(key.endswith(":lock") for key in redis.data)  # lock always released


async def test_redis_outage_fails_closed_for_keyed_requests_only() -> None:
    counter = Counter()
    async with client(wrap(build_app(counter), FakeRedis(fail=True))) as http:
        keyed = await http.post("/things", json={"a": 1}, headers=KEY)
        plain = await http.post("/things", json={"a": 1})
    assert keyed.status_code == 503
    assert plain.status_code == 201
    assert counter.calls == 1


async def test_stored_record_is_compact_json() -> None:
    redis = FakeRedis()
    async with client(wrap(build_app(Counter()), redis)) as http:
        await http.post("/things", json={"a": 1}, headers=KEY)
    (stored,) = [value for key, value in redis.data.items() if not key.endswith(":lock")]
    record: dict[str, Any] = json.loads(stored)
    assert record["status"] == 201
    assert set(record) == {"fingerprint", "status", "headers", "body"}


async def test_request_timeout_returns_504() -> None:
    async def slow(_: Request) -> Response:
        await asyncio.sleep(5)
        return Response("late")

    async def stream(_: Request) -> Response:
        await asyncio.sleep(0.1)
        return Response("stream ok")

    app = Starlette(routes=[Route("/slow", slow), Route("/api/v1/events", stream)])
    guarded = RequestTimeoutMiddleware(app, timeout_seconds=0.05, exempt_prefixes=("/api/v1/events",))
    async with client(guarded) as http:
        timed_out = await http.get("/slow")
        exempt = await http.get("/api/v1/events")
    assert timed_out.status_code == 504
    assert exempt.status_code == 200


class BrokenLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateDecision:
        self.calls += 1
        raise RedisConnectionError("down")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_limiter_falls_back_to_memory_and_stops_calling_redis() -> None:
    broken = BrokenLimiter()
    limiter = ResilientRateLimiter(broken, MemoryRateLimiter(), breaker=CircuitBreaker("test-rl", failure_threshold=2))
    decisions = [await limiter.hit("k", limit=3, window_seconds=60) for _ in range(4)]
    assert [d.allowed for d in decisions] == [True, True, True, False]
    assert decisions[0].remaining == 2
    assert decisions[3].reset_seconds >= 1
    assert broken.calls == 2  # circuit opened after two failures; Redis skipped afterwards


def test_circuit_breaker_opens_probes_and_closes() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker("test-cb", failure_threshold=2, reset_seconds=5, clock=clock)
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is CircuitState.CLOSED  # below threshold
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN
    assert not breaker.allow()
    clock.now += 5
    assert breaker.allow()  # one probe per cooldown...
    assert not breaker.allow()  # ...concurrent callers keep falling back
    breaker.record_failure()  # probe failed: stay open for another cooldown
    clock.now += 4.9
    assert not breaker.allow()
    clock.now += 0.1
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow()


def test_circuit_breaker_rejects_bad_config() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        CircuitBreaker("bad", failure_threshold=0)


async def test_idempotency_fails_fast_while_circuit_is_open() -> None:
    redis = FakeRedis(fail=True)
    counter = Counter()
    breaker = CircuitBreaker("test-idem-open", failure_threshold=1, reset_seconds=60)
    app = IdempotencyMiddleware(
        build_app(counter),
        redis=redis,  # type: ignore[arg-type]
        ttl_seconds=60,
        caller_key=lambda _: "caller",
        breaker=breaker,
    )
    async with client(app) as http:
        first = await http.post("/things", json={"a": 1}, headers=KEY)
        redis.fail = False  # even if Redis recovers, the open circuit short-circuits until the probe
        second = await http.post("/things", json={"a": 1}, headers=KEY)
    assert first.status_code == second.status_code == 503
    assert counter.calls == 0


async def test_memory_limiter_evicts_least_recently_used_keys() -> None:
    limiter = MemoryRateLimiter(max_keys=2)
    await limiter.hit("a", limit=1, window_seconds=60)
    await limiter.hit("b", limit=1, window_seconds=60)
    await limiter.hit("c", limit=1, window_seconds=60)  # evicts "a"
    assert (await limiter.hit("a", limit=1, window_seconds=60)).allowed


@pytest.mark.parametrize("bad_key", ["short", "has space here", "x" * 256, "semi;colon-key"])
async def test_malformed_keys_are_rejected(bad_key: str) -> None:
    counter = Counter()
    async with client(wrap(build_app(counter), FakeRedis())) as http:
        response = await http.post("/things", json={"a": 1}, headers={"Idempotency-Key": bad_key})
    assert response.status_code == 422
    assert counter.calls == 0
