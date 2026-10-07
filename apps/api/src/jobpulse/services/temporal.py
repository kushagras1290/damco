"""Temporal client helpers used by the API and the worker bootstrap."""

from __future__ import annotations

import asyncio

import structlog
from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError

from jobpulse.core.config import Settings
from jobpulse_core import workflow_names as names
from jobpulse_core.contracts import JobRef, PollingState
from jobpulse_core.errors import JobPulseError

logger = structlog.get_logger(__name__)


class WorkflowServiceError(JobPulseError):
    """Temporal unreachable or rejected the request."""

    retryable = True


async def connect(settings: Settings) -> Client:
    try:
        return await asyncio.wait_for(
            Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
                api_key=settings.temporal_api_key.get_secret_value() if settings.temporal_api_key else None,
                tls=settings.temporal_tls,
                data_converter=pydantic_data_converter,
            ),
            timeout=settings.temporal_connect_timeout_seconds,
        )
    except (TimeoutError, RPCError, RuntimeError) as exc:
        raise WorkflowServiceError(
            "cannot connect to Temporal", context={"address": settings.temporal_address}
        ) from exc


async def ensure_polling(client: Client, settings: Settings, source_id: str) -> WorkflowHandle[object, None]:
    """Start the per-source polling workflow if it is not already running (idempotent)."""
    try:
        return await client.start_workflow(
            names.SOURCE_POLLING_WORKFLOW,
            PollingState(source_id=source_id),
            id=names.polling_workflow_id(source_id),
            task_queue=settings.temporal_task_queue,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except RPCError as exc:
        raise WorkflowServiceError("failed to start polling workflow", context={"source_id": source_id}) from exc


async def trigger_sync(client: Client, settings: Settings, source_id: str) -> str:
    """Signal-with-start: wake the poller immediately (starting it if needed)."""
    try:
        handle = await client.start_workflow(
            names.SOURCE_POLLING_WORKFLOW,
            PollingState(source_id=source_id),
            id=names.polling_workflow_id(source_id),
            task_queue=settings.temporal_task_queue,
            start_signal=names.POLL_NOW_SIGNAL,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        )
    except RPCError as exc:
        raise WorkflowServiceError("failed to trigger sync", context={"source_id": source_id}) from exc
    return handle.id


async def stop_polling(client: Client, source_id: str) -> None:
    handle = client.get_workflow_handle(names.polling_workflow_id(source_id))
    try:
        await handle.signal(names.STOP_SIGNAL)
    except RPCError as exc:
        # Not running is fine: the source is disabled either way.
        logger.info("polling.stop_ignored", source_id=source_id, reason=str(exc))


async def rerun_job(client: Client, settings: Settings, ref: JobRef, *, nonce: str) -> str:
    """Force a fresh evaluation of one job for one profile (e.g. after a profile change)."""
    workflow_id = f"job-eval-{ref.job_id}-{ref.profile_id}-manual-{nonce}"
    try:
        await client.start_workflow(
            names.JOB_EVALUATION_WORKFLOW,
            ref,
            id=workflow_id,
            task_queue=settings.temporal_task_queue,
        )
    except RPCError as exc:
        raise WorkflowServiceError("failed to start evaluation", context={"job_id": ref.job_id}) from exc
    return workflow_id
