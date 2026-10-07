"""Workflow tests in Temporal's time-skipping test environment with stub activities."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import pytest
import pytest_asyncio
from temporalio import activity
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from jobpulse_core import workflow_names as names
from jobpulse_core.contracts import (
    EvaluationTarget,
    EvaluationTargets,
    FetchOutcome,
    JobRef,
    NormalizeInput,
    NormalizeOutcome,
    PollingState,
    PollRecord,
    RunFinish,
    RunRecord,
    SourceRef,
    SourceSchedule,
    StageResult,
    StoreInput,
    StoreOutcome,
)
from jobpulse_worker.workflows import ALL_WORKFLOWS

pytestmark = pytest.mark.workflow
TASK_QUEUE = "test-jobpulse"
WORKSPACE_A = str(uuid.uuid4())
WORKSPACE_B = str(uuid.uuid4())
TWO_TENANTS = (
    EvaluationTarget(workspace_id=WORKSPACE_A, profile_id=str(uuid.uuid4())),
    EvaluationTarget(workspace_id=WORKSPACE_B, profile_id=str(uuid.uuid4())),
)


def ref(job_id: str) -> JobRef:
    return JobRef(job_id=job_id, workspace_id=WORKSPACE_A, profile_id=TWO_TENANTS[0].profile_id)


@dataclass
class Recorder:
    calls: list[str] = field(default_factory=list)
    polls: list[PollRecord] = field(default_factory=list)
    finishes: list[RunFinish] = field(default_factory=list)
    eligible: bool = True
    enrichment_error: str | None = None
    fetch_error: str | None = None
    schedules_enabled: int = 1
    # The time-skipping test server does not auto-advance timers while abandoned
    # children are in flight; fan-out is covered by test_discovery_fans_out_evaluations.
    spawn_evaluations: bool = True
    targets: tuple[EvaluationTarget, ...] = TWO_TENANTS
    starts: list[RunRecord] = field(default_factory=list)


def stub_activities(rec: Recorder) -> list[object]:
    @activity.defn(name=names.RECORD_RUN_START)
    async def run_start(record: RunRecord) -> None:
        rec.calls.append(f"start:{record.workflow_type}")
        rec.starts.append(record)

    @activity.defn(name=names.RECORD_RUN_FINISH)
    async def run_finish(record: RunFinish) -> None:
        rec.finishes.append(record)

    def stage(name: str) -> object:
        @activity.defn(name=name)
        async def _stage(ref: JobRef) -> StageResult:
            rec.calls.append(name)
            if name == names.ELIGIBILITY:
                return StageResult(job_id=ref.job_id, proceed=rec.eligible, eligible=rec.eligible)
            if name == names.ENRICHMENT and rec.enrichment_error:
                raise ApplicationError("llm down", type=rec.enrichment_error, non_retryable=True)
            if name == names.RANKING:
                return StageResult(job_id=ref.job_id, proceed=True, eligible=True, score=0.82)
            return StageResult(job_id=ref.job_id, proceed=True, eligible=True)

        return _stage

    @activity.defn(name=names.GET_SOURCE_SCHEDULE)
    async def schedule(ref: SourceRef) -> SourceSchedule:
        enabled = rec.schedules_enabled > 0
        rec.schedules_enabled -= 1
        return SourceSchedule(
            source_id=ref.source_id,
            enabled=enabled,
            poll_interval_seconds=900,
            min_poll_interval_seconds=300,
            max_poll_interval_seconds=3600,
            consecutive_failures=0,
            circuit_open_remaining_seconds=0,
        )

    @activity.defn(name=names.FETCH_JOBS)
    async def fetch(ref: SourceRef) -> FetchOutcome:
        rec.calls.append("fetch")
        if rec.fetch_error:
            raise ApplicationError(
                "throttled", {"retry_after_seconds": 1200.0}, type=rec.fetch_error, non_retryable=True
            )
        return FetchOutcome(
            source_id=ref.source_id,
            source_kind="greenhouse",
            staging_key="staging/x/raw.json",
            discovered=2,
            skipped=0,
            not_modified=False,
            fetch_ms=5,
        )

    @activity.defn(name=names.NORMALIZE_JOBS)
    async def normalize(data: NormalizeInput) -> NormalizeOutcome:
        return NormalizeOutcome(
            source_id=data.source_id, normalized_key="staging/x/normalized.json", normalized=2, failed=0
        )

    @activity.defn(name=names.STORE_JOBS)
    async def store(data: StoreInput) -> StoreOutcome:
        ids = [str(uuid.uuid4()), str(uuid.uuid4())] if rec.spawn_evaluations else []
        return StoreOutcome(
            source_id=data.source_id,
            new_jobs=2,
            updated_jobs=0,
            unchanged_jobs=0,
            duplicate_jobs=0,
            closed_jobs=0,
            jobs_to_evaluate=ids,
            content_hashes={i: "a" * 64 for i in ids},
        )

    @activity.defn(name=names.RECORD_POLL)
    async def record_poll(record: PollRecord) -> None:
        rec.polls.append(record)

    @activity.defn(name=names.LIST_EVALUATION_TARGETS)
    async def targets(ref: SourceRef) -> EvaluationTargets:
        rec.calls.append(names.LIST_EVALUATION_TARGETS)
        return EvaluationTargets(targets=list(rec.targets))

    stages = [
        stage(n) for n in (names.ELIGIBILITY, names.ENRICHMENT, names.EMBEDDING, names.RANKING, names.NOTIFICATION)
    ]
    return [run_start, run_finish, schedule, fetch, normalize, store, record_poll, targets, *stages]


# Function-scoped: each test gets an isolated time-skipping server (~0.1 s startup), so
# abandoned child workflows from one test can never stall timers in another.
@pytest_asyncio.fixture
async def env() -> AsyncIterator[WorkflowEnvironment]:
    try:
        environment = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except RuntimeError as exc:  # test server download unavailable (offline CI)
        pytest.skip(f"Temporal test server unavailable: {exc}")
    async with environment:
        yield environment


async def run_worker(client: Client, rec: Recorder) -> Worker:
    return Worker(client, task_queue=TASK_QUEUE, workflows=ALL_WORKFLOWS, activities=stub_activities(rec))  # type: ignore[arg-type]


async def test_evaluation_runs_all_stages(env: WorkflowEnvironment) -> None:
    rec = Recorder()
    async with await run_worker(env.client, rec):
        summary = await env.client.execute_workflow(
            names.JOB_EVALUATION_WORKFLOW,
            ref("j1"),
            id=f"eval-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            result_type=None,
        )
    assert summary["eligible"] is True
    assert summary["score"] == 0.82
    assert rec.calls[1:] == [names.ELIGIBILITY, names.ENRICHMENT, names.EMBEDDING, names.RANKING, names.NOTIFICATION]
    assert rec.finishes[-1].status == "completed"


async def test_ineligible_job_stops_after_eligibility(env: WorkflowEnvironment) -> None:
    rec = Recorder(eligible=False)
    async with await run_worker(env.client, rec):
        await env.client.execute_workflow(
            names.JOB_EVALUATION_WORKFLOW,
            ref("j2"),
            id=f"eval-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert names.ENRICHMENT not in rec.calls
    assert names.RANKING not in rec.calls


async def test_llm_outage_degrades_but_still_ranks(env: WorkflowEnvironment) -> None:
    rec = Recorder(enrichment_error="IntelligenceUnavailableError")
    async with await run_worker(env.client, rec):
        await env.client.execute_workflow(
            names.JOB_EVALUATION_WORKFLOW,
            ref("j3"),
            id=f"eval-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert names.RANKING in rec.calls
    assert "EnrichmentActivity:degraded" in str(rec.finishes[-1].stats["stages"])


async def test_discovery_fans_out_evaluations(env: WorkflowEnvironment) -> None:
    rec = Recorder()
    async with await run_worker(env.client, rec):
        summary = await env.client.execute_workflow(
            names.SOURCE_DISCOVERY_WORKFLOW,
            SourceRef(source_id="s1"),
            id=f"disc-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        assert summary["evaluations_started"] == 4  # 2 new jobs x 2 subscribed profiles (2 workspaces)
        assert summary["new_jobs"] == 2


async def test_discovery_without_subscribers_starts_nothing(env: WorkflowEnvironment) -> None:
    rec = Recorder(targets=())
    async with await run_worker(env.client, rec):
        summary = await env.client.execute_workflow(
            names.SOURCE_DISCOVERY_WORKFLOW,
            SourceRef(source_id="s-orphan"),
            id=f"disc-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert summary["evaluations_started"] == 0
    assert names.LIST_EVALUATION_TARGETS in rec.calls


async def test_evaluation_runs_are_recorded_in_their_workspace(env: WorkflowEnvironment) -> None:
    rec = Recorder()
    async with await run_worker(env.client, rec):
        await env.client.execute_workflow(
            names.JOB_EVALUATION_WORKFLOW, ref("j5"), id=f"eval-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
    assert rec.starts[-1].workspace_id == WORKSPACE_A
    assert rec.finishes[-1].workspace_id == WORKSPACE_A


async def test_discovery_reports_rate_limit_without_failing(env: WorkflowEnvironment) -> None:
    rec = Recorder(fetch_error="SourceRateLimitedError")
    async with await run_worker(env.client, rec):
        summary = await env.client.execute_workflow(
            names.SOURCE_DISCOVERY_WORKFLOW,
            SourceRef(source_id="s2"),
            id=f"disc-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert summary["status"] == "failed"
    assert summary["error_type"] == "SourceRateLimitedError"
    assert summary["retry_after_seconds"] == 1200.0


async def test_polling_adapts_interval_and_stops_when_disabled(env: WorkflowEnvironment) -> None:
    rec = Recorder(schedules_enabled=2, spawn_evaluations=False)
    async with await run_worker(env.client, rec):
        await env.client.execute_workflow(
            names.SOURCE_POLLING_WORKFLOW,
            PollingState(source_id="s3"),
            id=f"poll-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert len(rec.polls) == 2
    assert all(p.success for p in rec.polls)
    assert rec.polls[0].next_interval_seconds == 300  # new jobs -> fastest


async def test_polling_backs_off_on_rate_limit(env: WorkflowEnvironment) -> None:
    rec = Recorder(schedules_enabled=1, fetch_error="SourceRateLimitedError")
    async with await run_worker(env.client, rec):
        await env.client.execute_workflow(
            names.SOURCE_POLLING_WORKFLOW,
            PollingState(source_id="s4"),
            id=f"poll-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
    assert rec.polls[0].success is False
    assert rec.polls[0].next_interval_seconds == 1800


async def test_failed_stage_marks_run_failed(env: WorkflowEnvironment) -> None:
    rec = Recorder()

    @activity.defn(name=names.ELIGIBILITY)
    async def broken(ref: JobRef) -> StageResult:
        raise ApplicationError("job gone", type="JobNotFoundError", non_retryable=True)

    activities = [
        a for a in stub_activities(rec) if getattr(a, "__temporal_activity_definition").name != names.ELIGIBILITY
    ]
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=ALL_WORKFLOWS, activities=[*activities, broken]):  # type: ignore[list-item]
        with pytest.raises(WorkflowFailureError):
            await env.client.execute_workflow(
                names.JOB_EVALUATION_WORKFLOW,
                ref("j9"),
                id=f"eval-{uuid.uuid4()}",
                task_queue=TASK_QUEUE,
            )
    assert rec.finishes[-1].status == "failed"
    assert "JobNotFoundError" in (rec.finishes[-1].error or "")
