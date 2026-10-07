"""Minimal circuit breaker for optional dependencies (Redis).

Without it, every request during an outage pays the full connect timeout (plus retries)
before falling back. With it, the first failures open the circuit and callers take the
fallback path immediately; after ``reset_seconds`` a single probe is let through, and a
success closes the circuit again.

Single event loop only (no locks needed): state changes happen between awaits.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import StrEnum

import structlog
from prometheus_client import Gauge

logger = structlog.get_logger(__name__)

CIRCUIT_OPEN = Gauge("circuit_open", "1 when the circuit for a dependency is open", ["name"])

DEFAULT_FAILURE_THRESHOLD = 2
DEFAULT_RESET_SECONDS = 5.0


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        reset_seconds: float = DEFAULT_RESET_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1 or reset_seconds <= 0:
            msg = "failure_threshold must be >= 1 and reset_seconds > 0"
            raise ValueError(msg)
        self.name = name
        self._threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        CIRCUIT_OPEN.labels(name=name).set(0)

    @property
    def state(self) -> CircuitState:
        return CircuitState.CLOSED if self._opened_at is None else CircuitState.OPEN

    def allow(self) -> bool:
        """True if the caller may try the dependency now (closed, or time for a probe)."""
        if self._opened_at is None:
            return True
        now = self._clock()
        if now - self._opened_at >= self._reset_seconds:
            self._opened_at = now  # one probe per cooldown; concurrent callers keep falling back
            return True
        return False

    def record_success(self) -> None:
        if self._opened_at is not None:
            logger.info("circuit.closed", name=self.name)
            CIRCUIT_OPEN.labels(name=self.name).set(0)
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= self._threshold:
            if self._opened_at is None:
                logger.warning("circuit.opened", name=self.name, failures=self._failures)
                CIRCUIT_OPEN.labels(name=self.name).set(1)
            self._opened_at = self._clock()
