"""SourcePollingWorkflow: one long-lived workflow per source implementing adaptive polling.

Loop: read schedule -> (circuit open? wait) -> run SourceDiscoveryWorkflow -> decide next
interval with the pure ``next_poll`` policy -> persist -> durable sleep (wakes early on
the ``poll_now`` signal). Uses continue-as-new to keep history bounded.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from jobpulse_core import workflow_names as names
    from jobpulse_core.contracts import DiscoveryResultSummary, PollingState, PollRecord, SourceRef, SourceSchedule
    from jobpulse_core.polling import PollOutcome, next_poll
    from jobpulse_worker.workflows.policies import BOOKKEEPING_RETRY, SHORT_TIMEOUT

ITERATIONS_BEFORE_CONTINUE_AS_NEW = 50
DISCOVERY_TIMEOUT = timedelta(minutes=30)
RATE_LIMIT_ERRORS = frozenset({"SourceRateLimitedError"})


def classify(summary: DiscoveryResultSummary) -> PollOutcome:
    if summary.status == "failed":
        return PollOutcome.RATE_LIMITED if summary.error_type in RATE_LIMIT_ERRORS else PollOutcome.FAILED
    if summary.new_jobs > 0 or summary.updated_jobs > 0:
        return PollOutcome.NEW_JOBS
    return PollOutcome.NO_CHANGE


@workflow.defn(name=names.SOURCE_POLLING_WORKFLOW)
class SourcePollingWorkflow:
    def __init__(self) -> None:
        self._poll_now = False
        self._stop = False
        self._last: DiscoveryResultSummary | None = None
        self._next_sleep_seconds = 0

    @workflow.signal(name=names.POLL_NOW_SIGNAL)
    def poll_now(self) -> None:
        self._poll_now = True

    @workflow.signal(name=names.STOP_SIGNAL)
    def stop(self) -> None:
        self._stop = True

    @workflow.query(name=names.STATUS_QUERY)
    def status(self) -> dict[str, object]:
        return {
            "next_sleep_seconds": self._next_sleep_seconds,
            "last": self._last.model_dump() if self._last else None,
            "stopping": self._stop,
        }

    async def _sleep(self, seconds: int) -> None:
        """Durable sleep that ends early on poll_now / stop signals."""
        self._next_sleep_seconds = seconds
        try:
            await workflow.wait_condition(lambda: self._poll_now or self._stop, timeout=timedelta(seconds=seconds))
        except TimeoutError:
            return
        finally:
            self._poll_now = False

    @workflow.run
    async def run(self, state: PollingState) -> None:
        failures = state.consecutive_failures
        for iteration in range(state.iterations, state.iterations + ITERATIONS_BEFORE_CONTINUE_AS_NEW):
            if self._stop:
                return
            schedule: SourceSchedule = await workflow.execute_activity(
                names.GET_SOURCE_SCHEDULE,
                SourceRef(source_id=state.source_id),
                result_type=SourceSchedule,
                start_to_close_timeout=SHORT_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
            )
            if not schedule.enabled:
                workflow.logger.info("source disabled; polling stops", extra={"source_id": state.source_id})
                return
            if schedule.circuit_open_remaining_seconds > 0 and not self._poll_now:
                await self._sleep(schedule.circuit_open_remaining_seconds)
                continue

            self._last = await workflow.execute_child_workflow(
                names.SOURCE_DISCOVERY_WORKFLOW,
                SourceRef(source_id=state.source_id),
                id=f"source-discovery-{state.source_id}-{workflow.info().run_id[:8]}-{iteration}",
                result_type=DiscoveryResultSummary,
                execution_timeout=DISCOVERY_TIMEOUT,
            )
            outcome = classify(self._last)
            failures = failures + 1 if outcome in {PollOutcome.FAILED, PollOutcome.RATE_LIMITED} else 0
            decision = next_poll(
                outcome=outcome,
                current_interval=schedule.poll_interval_seconds,
                min_interval=schedule.min_poll_interval_seconds,
                max_interval=schedule.max_poll_interval_seconds,
                consecutive_failures=failures,
                retry_after_seconds=self._last.retry_after_seconds,
            )
            await workflow.execute_activity(
                names.RECORD_POLL,
                PollRecord(
                    source_id=state.source_id,
                    success=outcome not in {PollOutcome.FAILED, PollOutcome.RATE_LIMITED},
                    new_jobs=self._last.new_jobs,
                    next_interval_seconds=decision.next_interval_seconds,
                    circuit_open_seconds=decision.sleep_seconds if decision.circuit_open else None,
                    error=self._last.error,
                ),
                start_to_close_timeout=SHORT_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
            )
            if decision.circuit_open:
                failures = 0
            workflow.logger.info(
                "poll complete",
                extra={"source_id": state.source_id, "outcome": outcome.value, "sleep": decision.sleep_seconds},
            )
            await self._sleep(decision.sleep_seconds)

        workflow.continue_as_new(
            PollingState(
                source_id=state.source_id,
                iterations=state.iterations + ITERATIONS_BEFORE_CONTINUE_AS_NEW,
                consecutive_failures=failures,
            ),
        )
