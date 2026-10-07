"""Worker entrypoint: connect to Temporal, register workflows/activities, ensure every
enabled source has a running polling workflow, expose Prometheus metrics, and shut
down gracefully on SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import timedelta

import sentry_sdk
import structlog
from prometheus_client import start_http_server
from temporalio.client import Client
from temporalio.worker import Worker

from jobpulse.core.aio import run as run_async
from jobpulse.core.config import Settings, get_settings
from jobpulse.core.logging import configure_logging
from jobpulse.core.telemetry import configure_tracing
from jobpulse.db.session import ping, transaction
from jobpulse.repositories.sources import SourceRepository
from jobpulse.services.context import AppContext
from jobpulse.services.temporal import WorkflowServiceError, connect, ensure_polling
from jobpulse_core.errors import JobPulseError
from jobpulse_worker.activities import build_activities
from jobpulse_worker.workflows import ALL_WORKFLOWS

logger = structlog.get_logger(__name__)

WORKER_METRICS_PORT = 9464
GRACEFUL_SHUTDOWN = timedelta(seconds=30)
MAX_CONCURRENT_ACTIVITIES = 20
STARTUP_DB_TIMEOUT_SECONDS = 15


async def _bootstrap_polling(ctx: AppContext, settings: Settings, client: Client) -> int:
    async with transaction(ctx.sessions) as session:
        sources = await SourceRepository(session).list(enabled=True, limit=10_000)
        source_ids = [str(source.id) for source in sources]
    started = 0
    for source_id in source_ids:
        try:
            await ensure_polling(client, settings, source_id)
            started += 1
        except WorkflowServiceError as exc:
            logger.warning("polling.bootstrap_failed", source_id=source_id, error=exc.message)
    return started


async def serve() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    configure_tracing(settings, service_name="jobpulse-worker")
    if settings.sentry_dsn:
        sentry_sdk.init(dsn=settings.sentry_dsn.get_secret_value(), environment=settings.environment)
    if settings.metrics_enabled:
        start_http_server(WORKER_METRICS_PORT)

    async with AppContext.create(settings) as ctx:
        await asyncio.wait_for(ping(ctx.engine), timeout=STARTUP_DB_TIMEOUT_SECONDS)
        client = await connect(settings)
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=ALL_WORKFLOWS,
            activities=build_activities(ctx),
            max_concurrent_activities=MAX_CONCURRENT_ACTIVITIES,
            graceful_shutdown_timeout=GRACEFUL_SHUTDOWN,
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows: rely on KeyboardInterrupt
                break

        async with worker:
            started = await _bootstrap_polling(ctx, settings, client)
            logger.info("worker.started", task_queue=settings.temporal_task_queue, polling_workflows=started)
            await stop.wait()
            logger.info("worker.stopping")


def run() -> None:
    try:
        run_async(serve())
    except KeyboardInterrupt:
        logger.info("worker.interrupted")
    except (JobPulseError, TimeoutError) as exc:
        logger.critical("worker.fatal", error=str(exc), error_type=type(exc).__name__)
        sys.exit(1)


if __name__ == "__main__":
    run()
