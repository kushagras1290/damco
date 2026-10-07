"""Exception hierarchy for JobPulse domain logic.

Every error raised by core code derives from :class:`JobPulseError` so callers can
catch the whole family without resorting to bare ``Exception``. The ``retryable``
flag is consumed by the Temporal worker to decide between retrying an activity and
failing it permanently.
"""

from __future__ import annotations


class JobPulseError(Exception):
    """Root of all JobPulse errors."""

    retryable: bool = False

    def __init__(self, message: str, *, context: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, object] = context or {}


class ConfigurationError(JobPulseError):
    """Invalid or missing configuration detected at startup or adapter construction."""


class ValidationError(JobPulseError):
    """Input failed domain validation."""


# --- Ingestion -----------------------------------------------------------------------


class SourceError(JobPulseError):
    """Base for failures talking to an external job source."""


class UnsafeUrlError(SourceError):
    """URL rejected by SSRF / allowlist protection. Never retryable."""


class RobotsDisallowedError(SourceError):
    """robots.txt forbids fetching the URL. Never retryable."""


class SourceFetchError(SourceError):
    """Transport-level failure (timeout, connection reset, 5xx). Retryable."""

    retryable = True


class SourceRateLimitedError(SourceFetchError):
    """Source returned HTTP 429 / throttled us. Retryable after backoff."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        context: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.retry_after_seconds = retry_after_seconds


class SourceResponseTooLargeError(SourceError):
    """Response exceeded the configured byte limit. Not retryable."""


class SourceParseError(SourceError):
    """Payload did not match the adapter's expected contract. Not retryable."""


class SourceNotFoundError(SourceError):
    """Board / feed does not exist (HTTP 404). Not retryable."""


# --- Intelligence ------------------------------------------------------------------------


class IntelligenceError(JobPulseError):
    """Base for LLM / embedding failures."""


class IntelligenceUnavailableError(IntelligenceError):
    """Provider unreachable or rate limited. Retryable."""

    retryable = True


class IntelligenceRefusalError(IntelligenceError):
    """Model refused or returned output that failed schema validation. Not retryable."""


# --- Notifications -------------------------------------------------------------------


class NotificationError(JobPulseError):
    """Base for notification delivery failures."""


class NotificationDeliveryError(NotificationError):
    """Transient delivery failure. Retryable."""

    retryable = True


class NotificationRejectedError(NotificationError):
    """Provider permanently rejected the message (4xx). Not retryable."""


# --- Storage -------------------------------------------------------------------------


class StorageError(JobPulseError):
    """Snapshot storage failure. Retryable."""

    retryable = True


class SnapshotNotFoundError(StorageError):
    """Requested snapshot key does not exist. Not retryable."""

    retryable = False
