"""Activity implementations.

Every activity converts :class:`JobPulseError` into a Temporal ``ApplicationError``
whose ``type`` is the exception class name and whose ``non_retryable`` flag mirrors
``JobPulseError.retryable``. Workflows rely on these types for backoff decisions.
"""

from __future__ import annotations

import functools
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

import structlog
from temporalio import activity
from temporalio.exceptions import ApplicationError

from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE, TenantScope, workspace_scope
from jobpulse.repositories.activity import WorkflowRunRepository
from jobpulse.services.context import AppContext
from jobpulse.services.evaluation import EvaluationService
from jobpulse.services.events import EventType, publish
from jobpulse.services.ingestion import IngestionService
from jobpulse_core import workflow_names as names
from jobpulse_core.contracts import (
    EvaluationTargets,
    FetchOutcome,
    JobRef,
    NormalizeInput,
    NormalizeOutcome,
    PollRecord,
    RunFinish,
    RunRecord,
    SourceRef,
    SourceSchedule,
    StageResult,
    StoreInput,
    StoreOutcome,
)
from jobpulse_core.errors import JobPulseError, SourceRateLimitedError

logger = structlog.get_logger(__name__)


def translate_errors[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await func(*args, **kwargs)
        except JobPulseError as exc:
            details: dict[str, Any] = {k: str(v) for k, v in exc.context.items()}
            if isinstance(exc, SourceRateLimitedError) and exc.retry_after_seconds is not None:
                details["retry_after_seconds"] = exc.retry_after_seconds
            info = activity.info()
            logger.warning(
                "activity.failed",
                activity=info.activity_type,
                workflow_id=info.workflow_id,
                attempt=info.attempt,
                error_type=type(exc).__name__,
                error=exc.message,
                retryable=exc.retryable,
            )
            raise ApplicationError(
                exc.message,
                details,
                type=type(exc).__name__,
                non_retryable=not exc.retryable,
            ) from exc

    return wrapper


class DiscoveryActivities:
    def __init__(self, ctx: AppContext) -> None:
        self._ingestion = IngestionService(ctx)

    @activity.defn(name=names.GET_SOURCE_SCHEDULE)
    @translate_errors
    async def get_schedule(self, ref: SourceRef) -> SourceSchedule:
        return await self._ingestion.schedule(ref.source_id)

    @activity.defn(name=names.FETCH_JOBS)
    @translate_errors
    async def fetch_jobs(self, ref: SourceRef) -> FetchOutcome:
        return await self._ingestion.fetch(ref.source_id)

    @activity.defn(name=names.NORMALIZE_JOBS)
    @translate_errors
    async def normalize_jobs(self, data: NormalizeInput) -> NormalizeOutcome:
        return await self._ingestion.normalize(data.source_id, data.staging_key)

    @activity.defn(name=names.STORE_JOBS)
    @translate_errors
    async def store_jobs(self, data: StoreInput) -> StoreOutcome:
        return await self._ingestion.store(data.source_id, data.normalized_key)

    @activity.defn(name=names.LIST_EVALUATION_TARGETS)
    @translate_errors
    async def list_evaluation_targets(self, ref: SourceRef) -> EvaluationTargets:
        return await self._ingestion.evaluation_targets(ref.source_id)

    @activity.defn(name=names.RECORD_POLL)
    @translate_errors
    async def record_poll(self, record: PollRecord) -> None:
        await self._ingestion.record_poll(record)


class EvaluationActivities:
    def __init__(self, ctx: AppContext) -> None:
        self._evaluation = EvaluationService(ctx)

    @activity.defn(name=names.ELIGIBILITY)
    @translate_errors
    async def eligibility(self, ref: JobRef) -> StageResult:
        return await self._evaluation.eligibility(ref, activity.info().workflow_id)

    @activity.defn(name=names.ENRICHMENT)
    @translate_errors
    async def enrichment(self, ref: JobRef) -> StageResult:
        return await self._evaluation.enrich(ref, activity.info().workflow_id)

    @activity.defn(name=names.EMBEDDING)
    @translate_errors
    async def embedding(self, ref: JobRef) -> StageResult:
        return await self._evaluation.embed(ref)

    @activity.defn(name=names.RANKING)
    @translate_errors
    async def ranking(self, ref: JobRef) -> StageResult:
        return await self._evaluation.rank(ref, activity.info().workflow_id)

    @activity.defn(name=names.NOTIFICATION)
    @translate_errors
    async def notification(self, ref: JobRef) -> StageResult:
        return await self._evaluation.notify(ref)


class RunActivities:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    @activity.defn(name=names.RECORD_RUN_START)
    @translate_errors
    async def start(self, record: RunRecord) -> None:
        async with transaction(self._ctx.sessions, scope=_run_scope(record.workspace_id)) as session:
            await WorkflowRunRepository(session).start(
                workflow_id=record.workflow_id,
                run_id=record.run_id,
                workflow_type=record.workflow_type,
                source_id=_uuid_or_none(record.source_id),
                job_id=_uuid_or_none(record.job_id),
            )
            if record.workflow_type in BROADCAST_RUN_TYPES:
                await publish(
                    session,
                    EventType.RUN_STARTED,
                    {
                        "workflow_type": record.workflow_type,
                        "workflow_id": record.workflow_id,
                        "source_id": record.source_id,
                    },
                )

    @activity.defn(name=names.RECORD_RUN_FINISH)
    @translate_errors
    async def finish(self, record: RunFinish) -> None:
        async with transaction(self._ctx.sessions, scope=_run_scope(record.workspace_id)) as session:
            await WorkflowRunRepository(session).finish(
                workflow_id=record.workflow_id,
                run_id=record.run_id,
                status=record.status,
                stats=dict(record.stats),
                error=record.error,
                now=datetime.now(tz=UTC),
            )
            # Evaluation runs are numerous; their successes are already visible as job.evaluated.
            if activity.info().workflow_type in BROADCAST_RUN_TYPES or record.status == "failed":
                await publish(
                    session,
                    EventType.RUN_FINISHED,
                    {
                        "workflow_type": activity.info().workflow_type,
                        "workflow_id": record.workflow_id,
                        "status": record.status,
                        "error": record.error,
                        "stats": dict(record.stats),
                    },
                )


BROADCAST_RUN_TYPES = frozenset({names.SOURCE_DISCOVERY_WORKFLOW})


def _run_scope(workspace_id: str | None) -> TenantScope:
    """Evaluation runs belong to their workspace; polling/discovery runs are catalogue (system)."""
    return workspace_scope(workspace_id) if workspace_id else SYSTEM_SCOPE


def _uuid_or_none(value: str | None) -> uuid.UUID | None:
    return uuid.UUID(value) if value else None


def build_activities(ctx: AppContext) -> Sequence[Callable[..., Any]]:
    discovery = DiscoveryActivities(ctx)
    evaluation = EvaluationActivities(ctx)
    runs = RunActivities(ctx)
    return [
        discovery.get_schedule,
        discovery.fetch_jobs,
        discovery.normalize_jobs,
        discovery.store_jobs,
        discovery.list_evaluation_targets,
        discovery.record_poll,
        evaluation.eligibility,
        evaluation.enrichment,
        evaluation.embedding,
        evaluation.ranking,
        evaluation.notification,
        runs.start,
        runs.finish,
    ]
