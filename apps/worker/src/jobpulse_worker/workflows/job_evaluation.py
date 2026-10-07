"""JobEvaluationWorkflow: Eligibility -> Enrichment -> Embedding -> Ranking -> Notification."""

from __future__ import annotations

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from jobpulse_core import workflow_names as names
    from jobpulse_core.contracts import EvaluationSummary, JobRef, RunFinish, RunRecord, StageResult
    from jobpulse_worker.workflows.policies import (
        BOOKKEEPING_RETRY,
        DEFAULT_RETRY,
        LLM_RETRY,
        LLM_TIMEOUT,
        NOTIFY_TIMEOUT,
        SHORT_TIMEOUT,
    )

# Enrichment / embeddings are optional: if the LLM is down after all retries we still
# rank with deterministic signals rather than dropping the job.
DEGRADABLE_STAGES = frozenset({names.ENRICHMENT, names.EMBEDDING})


def _cause_type(error: ActivityError) -> str:
    cause = error.cause
    return cause.type or "" if isinstance(cause, ApplicationError) else type(cause).__name__


@workflow.defn(name=names.JOB_EVALUATION_WORKFLOW)
class JobEvaluationWorkflow:
    def __init__(self) -> None:
        self._stages: list[str] = []
        self._eligible = False
        self._score: float | None = None
        self._notified = 0

    @workflow.query(name=names.STATUS_QUERY)
    def status(self) -> list[str]:
        return list(self._stages)

    async def _stage(self, name: str, ref: JobRef) -> StageResult | None:
        llm = name in DEGRADABLE_STAGES
        try:
            result: StageResult = await workflow.execute_activity(
                name,
                ref,
                result_type=StageResult,
                start_to_close_timeout=LLM_TIMEOUT
                if llm
                else (NOTIFY_TIMEOUT if name == names.NOTIFICATION else SHORT_TIMEOUT),
                retry_policy=LLM_RETRY if llm else DEFAULT_RETRY,
            )
        except ActivityError as error:
            if name in DEGRADABLE_STAGES:
                workflow.logger.warning("stage degraded", extra={"stage": name, "error_type": _cause_type(error)})
                self._stages.append(f"{name}:degraded")
                return None
            raise
        self._stages.append(f"{name}:{'ok' if result.proceed else 'stop'}")
        return result

    async def _evaluate(self, ref: JobRef) -> None:
        gate = await self._stage(names.ELIGIBILITY, ref)
        self._eligible = bool(gate and gate.proceed)
        if self._eligible:
            enriched = await self._stage(names.ENRICHMENT, ref)
            self._eligible = enriched is None or enriched.proceed
        if not self._eligible:
            return
        await self._stage(names.EMBEDDING, ref)
        ranked = await self._stage(names.RANKING, ref)
        self._score = ranked.score if ranked else None
        if ranked is not None and ranked.proceed:
            sent = await self._stage(names.NOTIFICATION, ref)
            self._notified = 1 if sent is not None and sent.proceed else 0

    async def _record_finish(self, status: str, error: str | None) -> None:
        info = workflow.info()
        await workflow.execute_activity(
            names.RECORD_RUN_FINISH,
            RunFinish(
                workflow_id=info.workflow_id,
                run_id=info.run_id,
                status=status,
                stats={
                    "eligible": self._eligible,
                    "score": self._score,
                    "notified": self._notified,
                    "stages": ",".join(self._stages),
                },
                error=error,
            ),
            start_to_close_timeout=SHORT_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
        )

    @workflow.run
    async def run(self, ref: JobRef) -> EvaluationSummary:
        info = workflow.info()
        await workflow.execute_activity(
            names.RECORD_RUN_START,
            RunRecord(
                workflow_id=info.workflow_id, run_id=info.run_id, workflow_type=info.workflow_type, job_id=ref.job_id
            ),
            start_to_close_timeout=SHORT_TIMEOUT,
            retry_policy=BOOKKEEPING_RETRY,
        )
        # Explicit success/failure paths, never `finally`: on worker cache eviction the
        # coroutine is closed and a finally-block could not schedule activities.
        try:
            await self._evaluate(ref)
        except ActivityError as exc:
            await self._record_finish("failed", f"{_cause_type(exc)}: {exc.cause}")
            raise
        await self._record_finish("completed", None)
        return EvaluationSummary(
            job_id=ref.job_id,
            eligible=self._eligible,
            score=self._score,
            notified=self._notified,
            stages=self._stages,
        )
