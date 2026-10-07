"""FastAPI application factory.

Run locally:  uv run uvicorn jobpulse.main:create_app --factory --reload
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from jobpulse.api.routes import applications, decisions, jobs, profile, runs, sources, system
from jobpulse.core.config import Settings, get_settings
from jobpulse.core.errors import AuthenticationError, install_exception_handlers
from jobpulse.core.logging import configure_logging
from jobpulse.core.middleware import BodySizeLimitMiddleware, RateLimitMiddleware, RequestContextMiddleware
from jobpulse.core.security import decode_token
from jobpulse.core.telemetry import configure_tracing
from jobpulse.services.context import AppContext

logger = structlog.get_logger(__name__)

API_TITLE = "JobPulse API"
API_VERSION = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    if settings.sentry_dsn:
        sentry_sdk.init(
            dsn=settings.sentry_dsn.get_secret_value(),
            environment=settings.environment,
            traces_sample_rate=settings.sentry_traces_sample_rate,
            send_default_pii=False,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        ctx = AppContext.create(settings)
        app.state.ctx = ctx
        app.state.temporal = None
        app.state.temporal_lock = asyncio.Lock()
        logger.info("api.started", environment=settings.environment, intelligence=ctx.intelligence is not None)
        try:
            yield
        finally:
            await ctx.aclose()
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
    install_exception_handlers(app)

    for router in (
        jobs.router,
        sources.router,
        runs.router,
        profile.router,
        applications.router,
        decisions.router,
        system.router,
    ):
        app.include_router(router)

    # Middleware executes outermost-last-added: context -> CORS -> rate limit -> body limit -> app.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)

    def identify(token: str) -> str | None:
        try:
            return decode_token(token, settings).subject
        except AuthenticationError:
            return None

    app.add_middleware(
        RateLimitMiddleware,
        per_minute=settings.rate_limit_per_minute,
        trusted_proxy_count=settings.trusted_proxy_count,
        identify=identify,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, hsts=is_prod)

    if configure_tracing(settings) is not None:
        FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready,metrics")
    return app
