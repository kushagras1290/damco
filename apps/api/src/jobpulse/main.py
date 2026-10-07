"""FastAPI application factory.

Run locally:  uv run uvicorn jobpulse.main:create_app --factory --reload
Production:   python -m jobpulse.serve
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from redis.asyncio import Redis
from starlette.datastructures import Headers
from starlette.types import Scope

from jobpulse.api.routes import applications, decisions, events, jobs, profile, runs, sources, system, workspaces
from jobpulse.core.cache import ResponseCache
from jobpulse.core.circuit import CircuitBreaker
from jobpulse.core.config import Settings, get_settings
from jobpulse.core.errors import AuthenticationError, install_exception_handlers
from jobpulse.core.idempotency import IdempotencyMiddleware
from jobpulse.core.logging import configure_logging
from jobpulse.core.middleware import (
    STREAM_PREFIX,
    BodySizeLimitMiddleware,
    CallerIdentity,
    RateLimitMiddleware,
    RateLimitPolicy,
    RequestContextMiddleware,
    RequestTimeoutMiddleware,
)
from jobpulse.core.ratelimit import MemoryRateLimiter, RedisRateLimiter, ResilientRateLimiter
from jobpulse.core.redis import build_redis, require_redis_for_api
from jobpulse.core.security import decode_token
from jobpulse.core.telemetry import configure_tracing
from jobpulse.services.context import AppContext
from jobpulse.services.event_hub import EventHub

logger = structlog.get_logger(__name__)

API_TITLE = "JobPulse API"
API_VERSION = "0.1.0"
GZIP_MIN_BYTES = 1024
CORS_MAX_AGE_SECONDS = 600
ROUTERS = (
    jobs.router,
    sources.router,
    runs.router,
    profile.router,
    applications.router,
    decisions.router,
    events.router,
    workspaces.router,
    system.router,
)


def _identity_resolver(settings: Settings) -> Callable[[str], CallerIdentity | None]:
    def identify(token: str) -> CallerIdentity | None:
        try:
            principal = decode_token(token, settings)
        except AuthenticationError:
            return None  # unverifiable tokens are limited by IP (and rejected by the auth layer)
        return CallerIdentity(key=principal.subject, authenticated=principal.authenticated)

    return identify


def _caller_key(identify: Callable[[str], CallerIdentity | None]) -> Callable[[Scope], str]:
    def caller_key(scope: Scope) -> str:
        _, _, token = Headers(scope=scope).get("authorization", "").partition(" ")
        identity = identify(token.strip()) if token else None
        client = scope.get("client")
        return identity.key if identity else f"ip:{client[0] if client else 'unknown'}"

    return caller_key


def _install_middleware(app: FastAPI, settings: Settings, redis: Redis | None, breaker: CircuitBreaker) -> None:
    """Outermost-last-added: context -> CORS -> gzip -> rate limit -> timeout -> body limit -> idempotency."""
    identify = _identity_resolver(settings)
    app.add_middleware(
        IdempotencyMiddleware,
        redis=redis,
        ttl_seconds=settings.idempotency_ttl_seconds,
        caller_key=_caller_key(identify),
        breaker=breaker,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(
        RequestTimeoutMiddleware, timeout_seconds=settings.request_timeout_seconds, exempt_prefixes=(STREAM_PREFIX,)
    )
    app.add_middleware(
        RateLimitMiddleware,
        limiter=ResilientRateLimiter(RedisRateLimiter(redis) if redis else None, MemoryRateLimiter(), breaker=breaker),
        policy=RateLimitPolicy(
            window_seconds=settings.rate_limit_window_seconds,
            anonymous=settings.rate_limit_anonymous,
            authenticated=settings.rate_limit_authenticated,
            writes=settings.rate_limit_writes,
            stream_connects=settings.rate_limit_stream_connects,
        ),
        trusted_proxy_count=settings.trusted_proxy_count,
        identify=identify,
    )
    app.add_middleware(GZipMiddleware, minimum_size=GZIP_MIN_BYTES)  # never compresses text/event-stream
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-Request-ID"],
            expose_headers=["RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "Retry-After", "X-Request-ID"],
            max_age=CORS_MAX_AGE_SECONDS,
        )
    app.add_middleware(RequestContextMiddleware, hsts=settings.environment == "production")


def _init_observability(settings: Settings) -> None:
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.get_secret_value(),
            environment=settings.environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            send_default_pii=False,
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _init_observability(settings)
    require_redis_for_api(settings)
    redis = build_redis(settings, client_name="jobpulse-api")
    if redis is None:
        logger.warning("redis.disabled", reason="REDIS_URL not set: per-instance limits, no cache/idempotency")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        ctx = AppContext.create(settings)
        app.state.ctx = ctx
        app.state.temporal = None
        app.state.temporal_lock = asyncio.Lock()
        hub = EventHub(settings.realtime_dsn, max_clients=settings.sse_max_clients, queue_size=settings.sse_queue_size)
        if not hub.enabled:
            logger.warning("events.disabled", reason="pooled DATABASE_URL without DATABASE_LISTEN_URL")
        await hub.start()
        app.state.events = hub
        logger.info("api.started", environment=settings.environment, intelligence=ctx.intelligence is not None)
        try:
            yield
        finally:
            await hub.stop()
            await ctx.aclose()
            if redis is not None:
                await redis.aclose()
            logger.info("api.stopped")

    is_prod = settings.environment == "production"
    app = FastAPI(
        title=API_TITLE,
        version=API_VERSION,
        lifespan=lifespan,
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    # One breaker per Redis client: an outage seen by any feature short-circuits all of them.
    breaker = CircuitBreaker("redis")
    app.state.redis = redis
    app.state.cache = ResponseCache(redis, ttl_seconds=settings.cache_ttl_seconds, breaker=breaker)
    install_exception_handlers(app)
    for router in ROUTERS:
        app.include_router(router)
    _install_middleware(app, settings, redis, breaker)
    if configure_tracing(settings) is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready,metrics")
    return app
