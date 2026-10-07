"""Shared activity timeouts and retry policies (bounded retries everywhere)."""

from __future__ import annotations

from datetime import timedelta

from temporalio.common import RetryPolicy

from jobpulse_core.workflow_names import NON_RETRYABLE_ERRORS

SHORT_TIMEOUT = timedelta(seconds=30)
FETCH_TIMEOUT = timedelta(minutes=3)
STORE_TIMEOUT = timedelta(minutes=10)
LLM_TIMEOUT = timedelta(minutes=3)
NOTIFY_TIMEOUT = timedelta(minutes=1)

DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=5,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)

# Rate limits are handled by the polling workflow's backoff, not by hammering retries.
FETCH_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
    non_retryable_error_types=[*NON_RETRYABLE_ERRORS, "SourceRateLimitedError"],
)

LLM_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=5),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=5),
    maximum_attempts=6,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)

BOOKKEEPING_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=10,
    non_retryable_error_types=NON_RETRYABLE_ERRORS,
)
