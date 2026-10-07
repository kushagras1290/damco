"""Production middleware against real Redis: distributed limits, idempotency, cache, CORS, gzip."""

from __future__ import annotations

import gzip
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr
from redis.asyncio import Redis

from jobpulse.api.deps import get_ctx, get_temporal
from jobpulse.core.config import Settings
from jobpulse.main import create_app
from jobpulse.services.context import AppContext
from tests.auth_helpers import make_token
from tests.integration.test_api import FakeTemporal

pytestmark = pytest.mark.integration

OWNER = {"Authorization": f"Bearer {make_token()}"}
MAX_DEGRADED_LATENCY_SECONDS = 0.15
SOURCE_BODY = {
    "kind": "greenhouse",
    "name": "Idempotent Co",
    "company_name": "Idempotent Co",
    "company_domain": "idempotent.example",
    "board_token": "idempotentco",
}


@asynccontextmanager
async def client_for(settings: Settings, ctx: AppContext) -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: ctx
    temporal = FakeTemporal()
    app.dependency_overrides[get_temporal] = lambda: temporal  # never depend on a live Temporal
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def visitor_token() -> str:
    return make_token(sub=f"visitor:{uuid.uuid4().hex}", login="visitor")


async def test_rate_limits_are_shared_across_instances(redis_settings: Settings, ctx: AppContext) -> None:
    limited = redis_settings.model_copy(update={"rate_limit_anonymous": 3})
    async with client_for(limited, ctx) as first, client_for(limited, ctx) as second:
        statuses = [
            (await first.get("/api/v1/me")).status_code,
            (await second.get("/api/v1/me")).status_code,
            (await first.get("/api/v1/me")).status_code,
            (await second.get("/api/v1/me")).status_code,
        ]
    assert statuses == [200, 200, 200, 429]  # one window enforced across both "replicas"


async def test_rate_limit_falls_back_when_redis_is_down(settings: Settings, ctx: AppContext) -> None:
    unreachable = settings.model_copy(
        update={
            "redis_url": SecretStr("redis://127.0.0.1:1/0"),  # nothing listens on port 1
            "rate_limit_anonymous": 5,
            "redis_connect_timeout_seconds": 0.2,
            "redis_socket_timeout_seconds": 0.2,
        }
    )
    async with client_for(unreachable, ctx) as client:
        status = (await client.get("/api/v1/system")).json()
        statuses: list[int] = []
        durations: list[float] = []
        for _ in range(5):
            started = time.perf_counter()
            statuses.append((await client.get("/api/v1/me")).status_code)
            durations.append(time.perf_counter() - started)
    redis_dep = next(dep for dep in status["dependencies"] if dep["name"] == "redis")
    assert redis_dep["ok"] is False
    assert statuses == [200, 200, 200, 200, 429]  # still limited, by the per-instance fallback
    # Circuit is open after the first failures: later requests skip Redis instead of
    # paying connect timeouts + retries on every call.
    assert max(durations[-3:]) < MAX_DEGRADED_LATENCY_SECONDS


async def test_visitor_tokens_get_their_own_read_only_bucket(redis_settings: Settings, ctx: AppContext) -> None:
    limited = redis_settings.model_copy(update={"rate_limit_anonymous": 1})
    alice = {"Authorization": f"Bearer {visitor_token()}"}
    bob = {"Authorization": f"Bearer {visitor_token()}"}
    async with client_for(limited, ctx) as client:
        me = await client.get("/api/v1/me", headers=alice)
        alice_again = await client.get("/api/v1/me", headers=alice)
        bob_first = await client.get("/api/v1/me", headers=bob)
        write = await client.post("/api/v1/sources", json=SOURCE_BODY, headers=bob)
    assert me.status_code == 200
    assert me.json()["role"] == "PUBLIC_DEMO"
    assert alice_again.status_code == 429
    assert bob_first.status_code == 200  # visitors behind one web server do not share a bucket
    assert write.status_code in {403, 429}  # read-only either way


async def test_visitor_tokens_are_not_a_login_when_demo_is_disabled(settings: Settings, ctx: AppContext) -> None:
    private = settings.model_copy(update={"public_demo_enabled": False})
    async with client_for(private, replace(ctx, settings=private)) as client:
        visitor = await client.get("/api/v1/me", headers={"Authorization": f"Bearer {visitor_token()}"})
        owner = await client.get("/api/v1/me", headers=OWNER)
    assert visitor.status_code == 401
    assert owner.status_code == 200


async def test_idempotent_create_is_replayed(redis_settings: Settings, ctx: AppContext) -> None:
    key = {"Idempotency-Key": f"create-{uuid.uuid4().hex}"}
    async with client_for(redis_settings, ctx) as client:
        first = await client.post("/api/v1/sources", json=SOURCE_BODY, headers={**OWNER, **key})
        replay = await client.post("/api/v1/sources", json=SOURCE_BODY, headers={**OWNER, **key})
        reused = await client.post("/api/v1/sources", json={**SOURCE_BODY, "name": "Other"}, headers={**OWNER, **key})
        listed = (await client.get("/api/v1/sources", headers=OWNER)).json()
    assert first.status_code == 201
    assert "idempotent-replayed" not in first.headers
    assert replay.status_code == 201
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json()["id"] == first.json()["id"]
    assert reused.status_code == 422
    assert reused.json()["type"].endswith("/idempotency_key_reused")
    names = [item["name"] for item in _items(listed)]
    assert names.count("Idempotent Co") == 1  # executed exactly once


async def test_idempotency_keys_are_scoped_per_caller(redis_settings: Settings, ctx: AppContext) -> None:
    key = {"Idempotency-Key": f"scoped-{uuid.uuid4().hex}"}
    other_owner = {"Authorization": f"Bearer {make_token(424242)}"}
    async with client_for(redis_settings, ctx) as client:
        mine = await client.patch("/api/v1/profile", json={"display_name": "Mine"}, headers={**OWNER, **key})
        theirs = await client.patch("/api/v1/profile", json={"display_name": "Mine"}, headers={**other_owner, **key})
    assert mine.status_code == 200
    assert "idempotent-replayed" not in theirs.headers  # never served another caller's response
    assert theirs.status_code == 403  # not an owner: executed (and rejected) on its own


async def test_idempotency_rejects_bad_keys_and_requires_redis(settings: Settings, ctx: AppContext) -> None:
    async with client_for(settings, ctx) as client:  # no REDIS_URL
        invalid = await client.post("/api/v1/sources", json=SOURCE_BODY, headers={**OWNER, "Idempotency-Key": "x"})
        keyed = await client.post(
            "/api/v1/sources", json=SOURCE_BODY, headers={**OWNER, "Idempotency-Key": uuid.uuid4().hex}
        )
        unkeyed = await client.patch("/api/v1/profile", json={"display_name": "Plain"}, headers=OWNER)
    assert invalid.status_code == 422
    assert keyed.status_code == 503  # never silently risk a duplicate
    assert keyed.headers["retry-after"] == "5"
    assert unkeyed.status_code == 200  # requests without a key are unaffected


async def test_dashboard_is_cached_in_redis(redis_settings: Settings, ctx: AppContext, redis_url: str) -> None:
    async with client_for(redis_settings, ctx) as client:
        first = await client.get("/api/v1/dashboard")
        second = await client.get("/api/v1/dashboard")
        status = (await client.get("/api/v1/system")).json()
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    redis = Redis.from_url(redis_url)
    try:
        assert await redis.exists("jp:cache:dashboard:v1") == 1
        assert 0 < await redis.pttl("jp:cache:dashboard:v1") <= 5000
    finally:
        await redis.aclose()
    redis_dep = next(dep for dep in status["dependencies"] if dep["name"] == "redis")
    assert redis_dep["ok"] is True


async def test_cors_is_deny_by_default(settings: Settings, ctx: AppContext) -> None:
    preflight = {"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    async with client_for(settings, ctx) as client:
        response = await client.options("/api/v1/sources", headers=preflight)
        simple = await client.get("/api/v1/jobs", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-origin" not in simple.headers


async def test_cors_allows_only_configured_origins(settings: Settings, ctx: AppContext) -> None:
    allowed = settings.model_copy(update={"cors_allowed_origins": ["https://app.example"]})
    async with client_for(allowed, ctx) as client:
        good = await client.options(
            "/api/v1/sources",
            headers={
                "Origin": "https://app.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "Idempotency-Key, Content-Type",
            },
        )
        bad = await client.options(
            "/api/v1/sources", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
        )
    assert good.status_code == 200
    assert good.headers["access-control-allow-origin"] == "https://app.example"
    assert "idempotency-key" in good.headers["access-control-allow-headers"].lower()
    assert bad.status_code == 400
    assert "access-control-allow-origin" not in bad.headers


async def test_large_responses_are_gzipped(settings: Settings, ctx: AppContext) -> None:
    async with client_for(settings, ctx) as client:
        response = await client.get("/openapi.json", headers={"Accept-Encoding": "gzip"})
        raw = await client.send(
            client.build_request("GET", "/openapi.json", headers={"Accept-Encoding": "gzip"}), stream=True
        )
        compressed = b"".join([chunk async for chunk in raw.aiter_raw()])
        await raw.aclose()
    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert gzip.decompress(compressed) == response.content


def _items(payload: object) -> list[dict[str, object]]:
    if isinstance(payload, dict):
        items = payload.get("items", [])
        return items if isinstance(items, list) else []
    return payload if isinstance(payload, list) else []
