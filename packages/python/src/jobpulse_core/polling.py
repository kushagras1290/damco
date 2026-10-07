"""Adaptive polling policy (pure, deterministic - safe to call inside Temporal workflows).

active source  -> shorter interval      quiet source       -> longer interval
HTTP 429       -> exponential backoff   repeated failure   -> circuit breaker
new job found  -> temporarily increase polling rate
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

INTERVAL_LADDER_SECONDS: tuple[int, ...] = (300, 600, 900, 1800, 3600)
CIRCUIT_BREAKER_THRESHOLD = 5
CIRCUIT_OPEN_SECONDS = 6 * 3600
MAX_BACKOFF_SECONDS = 4 * 3600


class PollOutcome(StrEnum):
    NEW_JOBS = "new_jobs"
    NO_CHANGE = "no_change"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PollDecision:
    next_interval_seconds: int
    sleep_seconds: int
    circuit_open: bool
    reason: str


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def _step(current: int, direction: int, low: int, high: int) -> int:
    ladder = [step for step in INTERVAL_LADDER_SECONDS if low <= step <= high] or [_clamp(current, low, high)]
    nearest = min(range(len(ladder)), key=lambda i: abs(ladder[i] - current))
    return ladder[_clamp(nearest + direction, 0, len(ladder) - 1)]


def next_poll(
    *,
    outcome: PollOutcome,
    current_interval: int,
    min_interval: int,
    max_interval: int,
    consecutive_failures: int,
    retry_after_seconds: float | None = None,
) -> PollDecision:
    """Decide the next polling interval. ``consecutive_failures`` includes this poll."""
    current = _clamp(current_interval, min_interval, max_interval)
    match outcome:
        case PollOutcome.NEW_JOBS:
            interval = min_interval
            return PollDecision(interval, interval, False, "new jobs: poll at fastest rate")
        case PollOutcome.NO_CHANGE:
            interval = _step(current, +1, min_interval, max_interval)
            return PollDecision(interval, interval, False, "quiet source: back off one step")
        case PollOutcome.RATE_LIMITED:
            backoff = int(min(MAX_BACKOFF_SECONDS, current * (2 ** max(1, consecutive_failures))))
            if retry_after_seconds:
                backoff = max(backoff, int(retry_after_seconds))
            interval = _clamp(max(current * 2, min_interval), min_interval, max_interval)
            return PollDecision(interval, backoff, False, "rate limited: exponential backoff")
        case PollOutcome.FAILED:
            if consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
                return PollDecision(max_interval, CIRCUIT_OPEN_SECONDS, True, "circuit open after repeated failures")
            backoff = int(min(MAX_BACKOFF_SECONDS, current * (2 ** (consecutive_failures - 1))))
            return PollDecision(current, max(backoff, min_interval), False, "failure: exponential backoff")
    msg = f"unhandled outcome {outcome}"
    raise ValueError(msg)
