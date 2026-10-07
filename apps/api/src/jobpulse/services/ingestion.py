"""Discovery pipeline: fetch -> normalize -> store, with staged snapshots between steps.

Each step is idempotent so Temporal can retry it safely:
* fetch writes a content-addressed staging snapshot (same payload => same key);
* normalize is a pure transform of a staging snapshot;
* store upserts on ``(source_id, external_id)`` and only creates versions when the
  content hash changes.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from jobpulse.core.metrics import (
    JOBS_DISCOVERED,
    SOURCE_FAILURES,
    SOURCE_FETCH_DURATION,
    SOURCE_PAYLOAD_NEAR_LIMIT,
    SUSPICIOUS_LISTINGS,
)
from jobpulse.db.models import Source
from jobpulse.db.session import transaction
from jobpulse.repositories.jobs import JobRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.services.context import AppContext
from jobpulse.services.events import EventType, publish
from jobpulse.services.storage import safe_segment
from jobpulse_core.contracts import (
    FetchOutcome,
    NormalizeOutcome,
    PollRecord,
    SourceSchedule,
    StoreOutcome,
)
from jobpulse_core.domain.models import NormalizedJob, RawJob, SourceDefinition, SourceKind
from jobpulse_core.errors import JobPulseError, SourceError, ValidationError
from jobpulse_core.ingestion.normalize import canonical_json, normalize_job, sha256_hex
from jobpulse_core.sources import build_source, required_hosts

logger = structlog.get_logger(__name__)

_RAW_LIST = TypeAdapter(list[RawJob])
# Skip closing when a listing loses more than half its open jobs at once (min 10 open).
SHRINK_GUARD_MIN_OPEN = 10
SHRINK_GUARD_MAX_DROP = 0.5
# Warn well before a growing board hits OUTBOUND_MAX_RESPONSE_BYTES (a hard, non-retryable failure).
PAYLOAD_WARN_RATIO = 0.75


class StagedJob(NormalizedJob):
    """Normalized job plus its raw adapter record (kept for the immutable per-job snapshot)."""

    raw_payload: dict[str, Any]


_STAGED_LIST = TypeAdapter(list[StagedJob])


class SourceUnavailableError(JobPulseError):
    """Source is disabled or missing; discovery should stop without retrying."""


def _timestamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%S%fZ")


def _definition(source: Source) -> SourceDefinition:
    try:
        return SourceDefinition.model_validate(source.config)
    except PydanticValidationError as exc:
        raise ValidationError("stored source config is invalid", context={"source_id": str(source.id)}) from exc


class IngestionService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    # ------------------------------------------------------------------ schedule

    async def schedule(self, source_id: str) -> SourceSchedule:
        async with transaction(self._ctx.sessions) as session:
            source = await SourceRepository(session).get(uuid.UUID(source_id))
            if source is None:
                return SourceSchedule(
                    source_id=source_id,
                    enabled=False,
                    poll_interval_seconds=3600,
                    min_poll_interval_seconds=300,
                    max_poll_interval_seconds=3600,
                    consecutive_failures=0,
                    circuit_open_remaining_seconds=0,
                )
            remaining = 0
            if source.circuit_open_until is not None:
                remaining = max(0, int((source.circuit_open_until - datetime.now(tz=UTC)).total_seconds()))
            return SourceSchedule(
                source_id=source_id,
                enabled=source.enabled,
                poll_interval_seconds=source.poll_interval_seconds,
                min_poll_interval_seconds=source.min_poll_interval_seconds,
                max_poll_interval_seconds=source.max_poll_interval_seconds,
                consecutive_failures=source.consecutive_failures,
                circuit_open_remaining_seconds=remaining,
            )

    # ------------------------------------------------------------------ fetch

    async def fetch(self, source_id: str) -> FetchOutcome:
        async with transaction(self._ctx.sessions) as session:
            repo = SourceRepository(session)
            source = await repo.get(uuid.UUID(source_id))
            if source is None or not source.enabled:
                raise SourceUnavailableError("source missing or disabled", context={"source_id": source_id})
            definition = _definition(source)
            if definition.kind is SourceKind.DEMO and not self._ctx.settings.demo_mode:
                raise SourceUnavailableError("demo source requires DEMO_MODE", context={"source_id": source_id})
            checkpoint = await repo.get_checkpoint(source.id)
            kind = source.kind
            source_name = source.name
            await publish(
                session, EventType.SOURCE_PROGRESS, {"source_id": source_id, "source": source_name, "stage": "fetching"}
            )

        log = logger.bind(source_id=source_id, source_kind=kind)
        started = time.perf_counter()
        http = self._ctx.source_http(required_hosts(definition))
        try:
            adapter = build_source(definition, http)
            result = await adapter.discover(checkpoint)
        except SourceError as exc:
            SOURCE_FAILURES.labels(source_kind=kind, error=type(exc).__name__).inc()
            log.warning("source.fetch_failed", error=exc.message, error_type=type(exc).__name__, **exc.context)
            raise
        finally:
            await http.aclose()
            SOURCE_FETCH_DURATION.labels(source_kind=kind).observe(time.perf_counter() - started)

        if result.not_modified:
            log.info("source.not_modified")
            await self._emit(
                EventType.SOURCE_PROGRESS, {"source_id": source_id, "source": source_name, "stage": "not_modified"}
            )
            return FetchOutcome(
                source_id=source_id,
                source_kind=kind,
                staging_key=None,
                discovered=0,
                skipped=0,
                not_modified=True,
                fetch_ms=result.fetch_ms,
            )

        body = _RAW_LIST.dump_json(result.jobs)
        key = f"staging/{safe_segment(source_id)}/{sha256_hex(body)[:32]}/raw.json"
        await self._ctx.store.put(key, body, "application/json")
        limit = self._ctx.settings.outbound_max_response_bytes
        if len(result.raw_payload) > limit * PAYLOAD_WARN_RATIO:
            SOURCE_PAYLOAD_NEAR_LIMIT.labels(source_kind=kind).inc()
            log.warning("source.payload_near_limit", bytes=len(result.raw_payload), limit=limit)
        if result.raw_payload:
            await self._store_page_snapshot(source_id, definition, result.raw_payload, result.content_type)
        async with transaction(self._ctx.sessions) as session:
            await SourceRepository(session).save_checkpoint(uuid.UUID(source_id), result.checkpoint)
            await publish(
                session,
                EventType.SOURCE_PROGRESS,
                {"source_id": source_id, "source": source_name, "stage": "fetched", "found": len(result.jobs)},
            )

        log.info(
            "source.fetched", discovered=len(result.jobs), skipped=len(result.skipped), fetch_ms=round(result.fetch_ms)
        )
        return FetchOutcome(
            source_id=source_id,
            source_kind=kind,
            staging_key=key,
            discovered=len(result.jobs),
            skipped=len(result.skipped),
            not_modified=False,
            fetch_ms=result.fetch_ms,
            skipped_reason=result.skipped[0]["reason"] if result.skipped else None,
        )

    async def _emit(self, event_type: EventType, data: dict[str, object]) -> None:
        async with transaction(self._ctx.sessions) as session:
            await publish(session, event_type, data)

    async def _store_page_snapshot(
        self, source_id: str, definition: SourceDefinition, payload: bytes, content_type: str
    ) -> None:
        """Content-addressed: an unchanged listing (most polls) is stored exactly once."""
        digest = sha256_hex(payload)
        extension = "json" if "json" in content_type else "html"
        page_key = f"pages/{safe_segment(definition.company_domain)}/{safe_segment(source_id)}/{digest}.{extension}"
        async with transaction(self._ctx.sessions) as session:
            if await JobRepository(session).snapshot_exists(page_key):
                return
        await self._ctx.store.put(page_key, payload, content_type)
        async with transaction(self._ctx.sessions) as session:
            await JobRepository(session).add_snapshot(
                source_id=uuid.UUID(source_id),
                job_id=None,
                key=page_key,
                snapshot_hash=digest,
                content_type=content_type,
                size_bytes=len(payload),
                fetched_at=datetime.now(tz=UTC),
            )

    # ------------------------------------------------------------------ normalize

    async def normalize(self, source_id: str, staging_key: str) -> NormalizeOutcome:
        raw_jobs = _RAW_LIST.validate_json(await self._ctx.store.get(staging_key))
        staged: list[StagedJob] = []
        failed = 0
        for raw in raw_jobs:
            try:
                normalized = normalize_job(raw)
            except ValidationError as exc:
                failed += 1
                logger.warning(
                    "job.normalize_failed", source_id=source_id, external_id=raw.external_id, error=exc.message
                )
                continue
            staged.append(StagedJob(**normalized.model_dump(), raw_payload=raw.model_dump(mode="json")))
        body = _STAGED_LIST.dump_json(staged)
        key = staging_key.rsplit("/", 1)[0] + "/normalized.json"
        await self._ctx.store.put(key, body, "application/json")
        return NormalizeOutcome(source_id=source_id, normalized_key=key, normalized=len(staged), failed=failed)

    # ------------------------------------------------------------------ store

    async def store(self, source_id: str, normalized_key: str) -> StoreOutcome:
        staged = _STAGED_LIST.validate_json(await self._ctx.store.get(normalized_key))
        now = datetime.now(tz=UTC)
        sid = uuid.UUID(source_id)
        new = updated = unchanged = duplicates = 0
        to_evaluate: list[str] = []
        hashes: dict[str, str] = {}
        new_jobs: list[dict[str, str]] = []

        async with transaction(self._ctx.sessions) as session:
            sources = SourceRepository(session)
            jobs = JobRepository(session)
            source = await sources.get(sid)
            if source is None:
                raise SourceUnavailableError("source vanished during discovery", context={"source_id": source_id})
            kind = source.kind
            seen: list[str] = []
            known_hashes = await jobs.content_hashes(sid)  # one query instead of one per job

            for item in staged:
                seen.append(item.external_id)
                if known_hashes.get(item.external_id) == item.content_hash:
                    unchanged += 1
                    continue
                existing = (
                    await jobs.get_by_external(sid, item.external_id) if item.external_id in known_hashes else None
                )

                company = await sources.upsert_company(name=item.company_name, domain=item.company_domain)
                snapshot_bytes = canonical_json(item.raw_payload).encode("utf-8")
                snapshot_key = (
                    f"raw/{safe_segment(kind)}/{safe_segment(item.company_domain)}/"
                    f"{safe_segment(item.external_id)}/{_timestamp(now)}.json"
                )
                await self._ctx.store.put(snapshot_key, snapshot_bytes, "application/json")

                if existing is None:
                    original = await jobs.find_duplicate(item, exclude_source_id=sid, company_id=company.id)
                    row = await jobs.insert(
                        source_id=sid,
                        company=company,
                        job=item,
                        duplicate_of_id=original.id if original else None,
                        now=now,
                    )
                    if original is None:
                        new += 1
                        new_jobs.append({"id": str(row.id), "title": row.title, "company": item.company_name})
                    else:
                        duplicates += 1
                        logger.info("job.duplicate", job_id=str(row.id), duplicate_of=str(original.id))
                else:
                    row = existing
                    await jobs.apply_update(row, item, now=now)
                    updated += 1

                snapshot = await jobs.add_snapshot(
                    source_id=sid,
                    job_id=row.id,
                    key=snapshot_key,
                    snapshot_hash=sha256_hex(snapshot_bytes),
                    content_type="application/json",
                    size_bytes=len(snapshot_bytes),
                    fetched_at=now,
                )
                await jobs.add_version(row, snapshot_id=snapshot.id)
                if row.duplicate_of_id is None:
                    to_evaluate.append(str(row.id))
                    hashes[str(row.id)] = row.content_hash

            await jobs.touch_seen(sid, seen, now)
            closed = await self._close_missing_guarded(jobs, sid, seen, now, source_id)
            await publish(
                session,
                EventType.JOBS_DISCOVERED if new or updated else EventType.SOURCE_PROGRESS,
                {
                    "source_id": source_id,
                    "source": source.name,
                    "stage": "stored",
                    "new": new,
                    "updated": updated,
                    "closed": closed,
                    "evaluating": len(to_evaluate),
                    "jobs": new_jobs,
                },
            )

        if new:
            JOBS_DISCOVERED.labels(source_kind=kind).inc(new)
        logger.info(
            "source.stored",
            source_id=source_id,
            new=new,
            updated=updated,
            unchanged=unchanged,
            duplicates=duplicates,
            closed=closed,
        )
        return StoreOutcome(
            source_id=source_id,
            new_jobs=new,
            updated_jobs=updated,
            unchanged_jobs=unchanged,
            duplicate_jobs=duplicates,
            closed_jobs=closed,
            jobs_to_evaluate=to_evaluate,
            content_hashes=hashes,
        )

    @staticmethod
    async def _close_missing_guarded(
        jobs: JobRepository, sid: uuid.UUID, seen: list[str], now: datetime, source_id: str
    ) -> int:
        """Close jobs absent from the listing - unless the listing shrank suspiciously.

        A transient upstream glitch (empty or truncated board) must not mass-close jobs;
        genuinely removed jobs are closed on the next healthy poll.
        """
        open_before = await jobs.open_count(sid)
        if open_before >= SHRINK_GUARD_MIN_OPEN and len(seen) < open_before * (1 - SHRINK_GUARD_MAX_DROP):
            SUSPICIOUS_LISTINGS.labels(reason="shrink").inc()
            logger.warning(
                "source.listing_shrink_suspected", source_id=source_id, open_before=open_before, seen=len(seen)
            )
            return 0
        return await jobs.close_missing(sid, seen, now) if seen else 0

    # ------------------------------------------------------------------ poll bookkeeping

    async def record_poll(self, record: PollRecord) -> None:
        now = datetime.now(tz=UTC)
        async with transaction(self._ctx.sessions) as session:
            repo = SourceRepository(session)
            source = await repo.get(uuid.UUID(record.source_id), for_update=True)
            if source is None:
                return
            interval = max(
                source.min_poll_interval_seconds, min(source.max_poll_interval_seconds, record.next_interval_seconds)
            )
            circuit_until = (
                now + timedelta(seconds=record.circuit_open_seconds) if record.circuit_open_seconds else None
            )
            await repo.record_poll(
                source,
                now=now,
                success=record.success,
                new_jobs=record.new_jobs,
                next_interval_seconds=interval,
                error=record.error,
                circuit_open_until=circuit_until,
            )
            await publish(
                session,
                EventType.SOURCE_POLLED,
                {
                    "source_id": record.source_id,
                    "source": source.name,
                    "success": record.success,
                    "next_poll_seconds": interval,
                    "circuit_open": circuit_until is not None,
                    "error": record.error,
                },
            )
