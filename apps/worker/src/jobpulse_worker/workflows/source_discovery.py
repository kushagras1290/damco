"""SourceDiscoveryWorkflow: FetchJobs -> NormalizeJobs -> StoreJobs -> start evaluations."""

from __future__ import annotations

from temporalio import workflow
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError
from temporalio.workflow import ParentClosePolicy

with workflow.unsafe.imports_passed_through():
    from jobpulse_core import workflow_names as names
    from jobpulse_core.contracts import (
        DiscoveryResultSummary,
        FetchOutcome,
        JobRef,
        NormalizeInput,
        NormalizeOutcome,
        RunFinish,
        RunRecord,
        SourceRef,
        StoreInput,
        StoreOutcome,
    )
    from jobpulse_worker.workflows.policies import (
        BOOKKEEPING_RETRY,
        DEFAULT_RETRY,
        FETCH_RETRY,
        FETCH_TIMEOUT,
        SHORT_TIMEOUT,
        STORE_TIMEOUT,
    )


def _failure(error: ActivityError) -> tuple[str, str, float | None]:
    cause = error.cause
    if isinstance(cause, ApplicationError):
        retry_after: float | None = None
        if cause.details and isinstance(cause.details[0], dict):
            raw = cause.details[0].get("retry_after_seconds")
            retry_after = float(raw) if isinstance(raw, (int, float)) else None
        return cause.type or "ApplicationError", cause.message, retry_after
    return type(cause).__name__ if cause else "ActivityError", str(error), None


@workflow.defn(name=names.SOURCE_DISCOVERY_WORKFLOW)
class SourceDiscoveryWorkflow:
    @workflow.run
    async def run(self, ref: SourceRef) -> DiscoveryResultSummary:
        info = workflow.info()
        await workflow.execute_activity(
            names.RECORD_RUN_START,
            RunRecord(
                workflow_id=info.workflow_id,
                run_id=info.run_id,
                workflow_type=info.workflow_type,
                source_id=ref.source_id,
            ),
            start_to_close_timeout=SHORT_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
        )
        summary: DiscoveryResultSummary
        try:
            summary = await self._discover(ref)
        except ActivityError as error:
            error_type, message, retry_after = _failure(error)
            summary = DiscoveryResultSummary(
                source_id=ref.source_id,
                status="failed",
                error=message[:1000],
                error_type=error_type,
                retry_after_seconds=retry_after,
            )
        await workflow.execute_activity(
            names.RECORD_RUN_FINISH,
            RunFinish(
                workflow_id=info.workflow_id,
                run_id=info.run_id,
                status="completed" if summary.status != "failed" else "failed",
                stats={
                    "new_jobs": summary.new_jobs,
                    "updated_jobs": summary.updated_jobs,
                    "evaluations_started": summary.evaluations_started,
                    "not_modified": summary.not_modified,
                    "error_type": summary.error_type,
                },
                error=summary.error,
            ),
            start_to_close_timeout=SHORT_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
        )
        return summary

    async def _discover(self, ref: SourceRef) -> DiscoveryResultSummary:
        fetched: FetchOutcome = await workflow.execute_activity(
            names.FETCH_JOBS,
            ref,
            result_type=FetchOutcome,
            start_to_close_timeout=FETCH_TIMEOUT,
            retry_policy=FETCH_RETRY,
        )
        if fetched.not_modified or fetched.staging_key is None:
            return DiscoveryResultSummary(source_id=ref.source_id, status="not_modified", not_modified=True)

        normalized: NormalizeOutcome = await workflow.execute_activity(
            names.NORMALIZE_JOBS,
            NormalizeInput(source_id=ref.source_id, staging_key=fetched.staging_key),
            result_type=NormalizeOutcome,
            start_to_close_timeout=STORE_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )
        stored: StoreOutcome = await workflow.execute_activity(
            names.STORE_JOBS,
            StoreInput(source_id=ref.source_id, normalized_key=normalized.normalized_key),
            result_type=StoreOutcome,
            start_to_close_timeout=STORE_TIMEOUT,
            retry_policy=DEFAULT_RETRY,
        )

        started = 0
        for job_id in stored.jobs_to_evaluate:
            content_hash = stored.content_hashes.get(job_id, "")
            try:
                await workflow.start_child_workflow(
                    names.JOB_EVALUATION_WORKFLOW,
                    JobRef(job_id=job_id, content_hash=content_hash or None),
                    id=names.evaluation_workflow_id(job_id, content_hash or "manual"),
                    parent_close_policy=ParentClosePolicy.ABANDON,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                )
                started += 1
            except WorkflowAlreadyStartedError:
                # Same job+content already being evaluated: idempotent no-op.
                workflow.logger.info("evaluation already running", extra={"job_id": job_id})
        return DiscoveryResultSummary(
            source_id=ref.source_id,
            status="completed",
            new_jobs=stored.new_jobs,
            updated_jobs=stored.updated_jobs,
            evaluations_started=started,
        )
