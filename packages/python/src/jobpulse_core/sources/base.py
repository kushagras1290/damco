"""Source adapter contract and shared helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError as PydanticValidationError

from jobpulse_core.domain.models import RawJob, SourceCheckpoint, SourceDefinition
from jobpulse_core.errors import SourceParseError
from jobpulse_core.ingestion.http import FetchResult, SafeHttpClient

MAX_JOBS_PER_DISCOVERY = 5000


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Adapter output. ``not_modified`` means the source answered 304 and ``jobs`` is empty."""

    jobs: list[RawJob]
    checkpoint: SourceCheckpoint
    not_modified: bool = False
    raw_payload: bytes = b""
    content_type: str = "application/json"
    fetch_ms: float = 0.0
    skipped: list[dict[str, str]] = field(default_factory=list)


@runtime_checkable
class JobSource(Protocol):
    """Every adapter discovers the current set of open jobs for one source."""

    definition: SourceDefinition

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult: ...


class BaseSource:
    """Shared plumbing: conditional GET, JSON decoding, defensive RawJob construction."""

    def __init__(self, definition: SourceDefinition, http: SafeHttpClient) -> None:
        self.definition = definition
        self._http = http

    async def _conditional_get(self, url: str, checkpoint: SourceCheckpoint) -> FetchResult:
        return await self._http.fetch(
            url,
            headers={"Accept": "application/json, application/xml;q=0.9, text/html;q=0.8"},
            etag=checkpoint.etag,
            last_modified=checkpoint.last_modified,
        )

    @staticmethod
    def _next_checkpoint(previous: SourceCheckpoint, result: FetchResult, ids: list[str]) -> SourceCheckpoint:
        return SourceCheckpoint(
            etag=result.headers.get("etag") or previous.etag,
            last_modified=result.headers.get("last-modified") or previous.last_modified,
            cursor=previous.cursor,
            last_seen_external_ids=ids if ids else previous.last_seen_external_ids,
        )

    @staticmethod
    def _decode_json(result: FetchResult) -> Any:
        try:
            return json.loads(result.content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SourceParseError("response is not valid JSON", context={"url": result.url}) from exc

    def _not_modified(self, checkpoint: SourceCheckpoint, result: FetchResult) -> DiscoveryResult:
        return DiscoveryResult(jobs=[], checkpoint=checkpoint, not_modified=True, fetch_ms=result.elapsed_ms)

    def _build(self, skipped: list[dict[str, str]], **fields: Any) -> RawJob | None:
        """Construct a RawJob; record (not raise) per-item contract violations."""
        fields.setdefault("company_name", self.definition.company_name)
        fields.setdefault("company_domain", self.definition.company_domain)
        try:
            return RawJob(**fields)
        except PydanticValidationError as exc:
            skipped.append(
                {
                    "external_id": str(fields.get("external_id", "?")),
                    "reason": exc.errors(include_url=False)[0]["msg"] if exc.errors() else "invalid",
                },
            )
            return None

    def _finish(
        self,
        checkpoint: SourceCheckpoint,
        result: FetchResult,
        jobs: list[RawJob],
        skipped: list[dict[str, str]],
    ) -> DiscoveryResult:
        if len(jobs) > MAX_JOBS_PER_DISCOVERY:
            jobs = jobs[:MAX_JOBS_PER_DISCOVERY]
        return DiscoveryResult(
            jobs=jobs,
            checkpoint=self._next_checkpoint(checkpoint, result, [job.external_id for job in jobs]),
            raw_payload=result.content,
            content_type=result.content_type,
            fetch_ms=result.elapsed_ms,
            skipped=skipped,
        )


def parse_datetime(value: Any) -> datetime | None:
    """Accept ISO-8601 strings and epoch seconds / milliseconds. Returns aware UTC datetimes."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=UTC)
    if isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def dig(payload: Any, dotted_path: str) -> Any:
    """Resolve ``a.b.0.c`` against nested dicts / lists. Returns None when missing."""
    current = payload
    for part in dotted_path.split("."):
        if part == "":
            continue
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current
