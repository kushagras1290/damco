"""Health probes, Prometheus metrics, dashboard aggregates and system status."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from temporalio.client import Client
from temporalio.service import RPCError

from jobpulse.api.deps import Ctx, Session
from jobpulse.api.mappers import job_summary
from jobpulse.api.schemas import DashboardStats, DependencyStatus, SystemStatus
from jobpulse.core.cache import ResponseCache
from jobpulse.core.errors import ServiceUnavailableError
from jobpulse.core.security import Reader
from jobpulse.db.session import ping
from jobpulse.repositories.activity import WorkflowRunRepository
from jobpulse.repositories.decisions import DecisionRepository
from jobpulse.repositories.jobs import JobFilters, JobRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.services.event_hub import EventHub
from jobpulse.services.temporal import WorkflowServiceError, connect

router = APIRouter(tags=["system"])

API_VERSION = "0.1.0"
READINESS_TIMEOUT_SECONDS = 3.0
DASHBOARD_DAYS = 14
TOP_MATCHES = 5


@router.get("/health/live", include_in_schema=False)
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False)
async def ready(ctx: Ctx) -> dict[str, str]:
    try:
        await asyncio.wait_for(ping(ctx.engine), timeout=READINESS_TIMEOUT_SECONDS)
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        raise ServiceUnavailableError("database not ready") from exc
    return {"status": "ready"}


@router.get("/metrics", include_in_schema=False)
async def metrics(ctx: Ctx) -> Response:
    if not ctx.settings.metrics_enabled:
        return Response(status_code=404)
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.get("/api/v1/dashboard", response_model=DashboardStats)
async def dashboard(request: Request, _: Reader, session: Session) -> DashboardStats:
    """Viewer-independent aggregates: served from the shared short-TTL cache when warm."""
    cache: ResponseCache = request.app.state.cache
    return await cache.get_or_compute("dashboard:v1", DashboardStats, lambda: _dashboard(session))


async def _dashboard(session: Session) -> DashboardStats:
    now = datetime.now(tz=UTC)
    since = now - timedelta(days=DASHBOARD_DAYS)
    jobs = JobRepository(session)
    decisions = DecisionRepository(session)
    top, _total = await jobs.search(
        JobFilters(eligibility_status="eligible", sort="score"), limit=TOP_MATCHES, offset=0
    )
    return DashboardStats(
        jobs_by_status=await jobs.count_by_status(),
        sources_total=await SourceRepository(session).count(),
        discovered_per_day=[
            {"day": day.date().isoformat(), "count": n} for day, n in await jobs.discovered_per_day(since)
        ],
        score_histogram=[{"bucket": bucket, "count": n} for bucket, n in await decisions.score_histogram()],
        rejection_reasons=[{"rule": rule, "count": n} for rule, n in await decisions.rejection_reasons(since)],
        runs_last_24h=await WorkflowRunRepository(session).status_counts(now - timedelta(hours=24)),
        notifications=await decisions.notification_counts(),
        llm_cost_usd=float(await decisions.total_llm_cost()),
        top_matches=[job_summary(row) for row in top],
    )


async def _temporal_status(request: Request, ctx: Ctx) -> DependencyStatus:
    client: Client | None = getattr(request.app.state, "temporal", None)
    try:
        if client is None:
            client = await connect(ctx.settings)
            request.app.state.temporal = client
        healthy = await asyncio.wait_for(client.service_client.check_health(), timeout=READINESS_TIMEOUT_SECONDS)
    except (WorkflowServiceError, TimeoutError, RPCError) as exc:
        return DependencyStatus(name="temporal", ok=False, detail=type(exc).__name__)
    return DependencyStatus(name="temporal", ok=bool(healthy), detail=ctx.settings.temporal_address)


def _realtime_status(request: Request) -> DependencyStatus:
    hub: EventHub = request.app.state.events
    if not hub.enabled:
        return DependencyStatus(name="realtime", ok=False, detail="disabled (pooled DB without DATABASE_LISTEN_URL)")
    detail = f"{hub.subscriber_count} live client(s)" if hub.connected else "reconnecting"
    return DependencyStatus(name="realtime", ok=hub.connected, detail=detail)


@router.get("/api/v1/system", response_model=SystemStatus)
async def system_status(request: Request, _: Reader, ctx: Ctx) -> SystemStatus:
    cache: ResponseCache = request.app.state.cache
    return await cache.get_or_compute("system:v1", SystemStatus, lambda: _system_status(request, ctx))


async def _redis_status(request: Request) -> DependencyStatus:
    redis: Redis | None = request.app.state.redis
    if redis is None:
        return DependencyStatus(name="redis", ok=False, detail="not configured (per-instance limits, no cache)")
    try:
        await asyncio.wait_for(redis.ping(), timeout=READINESS_TIMEOUT_SECONDS)
    except (TimeoutError, RedisError, OSError) as exc:
        return DependencyStatus(name="redis", ok=False, detail=f"{type(exc).__name__} (degraded: fail-open)")
    return DependencyStatus(name="redis", ok=True, detail="reachable")


async def _system_status(request: Request, ctx: Ctx) -> SystemStatus:
    settings = ctx.settings
    try:
        await asyncio.wait_for(ping(ctx.engine), timeout=READINESS_TIMEOUT_SECONDS)
        database = DependencyStatus(name="postgresql", ok=True, detail="reachable")
    except (TimeoutError, OSError, SQLAlchemyError) as exc:
        database = DependencyStatus(name="postgresql", ok=False, detail=type(exc).__name__)
    return SystemStatus(
        environment=settings.environment,
        version=API_VERSION,
        intelligence_enabled=ctx.intelligence is not None,
        storage_backend=settings.storage_backend,
        models={
            "classification": settings.openai_classification_model,
            "reasoning": settings.openai_reasoning_model,
            "embedding": settings.openai_embedding_model,
        },
        dependencies=[
            database,
            await _temporal_status(request, ctx),
            await _redis_status(request),
            _realtime_status(request),
        ],
    )
