"""Jobs (shared catalogue), versions, raw snapshots and per-profile job state.

Catalogue rows (jobs, versions, snapshots) are shared by every workspace. A profile's view
of a job - eligibility, score, evaluation progress - lives in ``profile_jobs`` and is
protected by row-level security, so queries joining it only ever see the current workspace.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import ColumnElement, Select, and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from jobpulse.db.models import Company, Job, JobVersion, ProfileJob, RawSnapshot, Source
from jobpulse_core.domain.models import NormalizedJob

TRIGRAM_DUPLICATE_THRESHOLD = 0.85
SortKey = Literal["score", "published", "discovered"]
PENDING = "pending"


@dataclass(frozen=True, slots=True)
class JobView:
    """A catalogue job plus one profile's state for it (None until first evaluated)."""

    job: Job
    state: ProfileJob | None

    @property
    def eligibility_status(self) -> str:
        return self.state.eligibility_status if self.state else PENDING

    @property
    def match_score(self) -> float | None:
        return self.state.match_score if self.state else None

    @property
    def workflow_state(self) -> str:
        if self.job.workflow_state != "discovered" or self.state is None:
            return self.job.workflow_state  # catalogue states (duplicate / closed) win
        return self.state.state


@dataclass(frozen=True, slots=True)
class JobFilters:
    query: str | None = None
    eligibility_status: str | None = None
    remote_policy: str | None = None
    seniority: str | None = None
    source_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    min_score: float | None = None
    include_closed: bool = False
    include_duplicates: bool = False
    sort: SortKey = "score"


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ reads

    async def get(self, job_id: uuid.UUID, *, for_update: bool = False) -> Job | None:
        statement = select(Job).where(Job.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_detail(self, job_id: uuid.UUID) -> Job | None:
        statement = (
            select(Job)
            .options(joinedload(Job.company), joinedload(Job.source).joinedload(Source.company))
            .where(Job.id == job_id)
        )
        return (await self._session.execute(statement)).unique().scalar_one_or_none()

    async def content_hashes(self, source_id: uuid.UUID) -> dict[str, str]:
        """external_id -> content_hash for every job of a source, in one query."""
        statement = select(Job.external_id, Job.content_hash).where(Job.source_id == source_id)
        return {row[0]: row[1] for row in (await self._session.execute(statement)).all()}

    async def open_count(self, source_id: uuid.UUID) -> int:
        statement = select(func.count(Job.id)).where(Job.source_id == source_id, Job.closed_at.is_(None))
        return int((await self._session.execute(statement)).scalar_one())

    async def snapshot_exists(self, key: str) -> bool:
        statement = select(RawSnapshot.id).where(RawSnapshot.snapshot_key == key).limit(1)
        return (await self._session.execute(statement)).first() is not None

    async def get_by_external(self, source_id: uuid.UUID, external_id: str) -> Job | None:
        statement = select(Job).where(Job.source_id == source_id, Job.external_id == external_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    def _filtered(self, filters: JobFilters, profile_id: uuid.UUID) -> Select[Job, ProfileJob]:
        statement = (
            select(Job, ProfileJob)
            .outerjoin(ProfileJob, and_(ProfileJob.job_id == Job.id, ProfileJob.profile_id == profile_id))
            .options(joinedload(Job.company), joinedload(Job.source).joinedload(Source.company))
        )
        conditions = []
        if filters.query:
            ts_query = func.websearch_to_tsquery("english", filters.query)
            conditions.append(
                or_(Job.search_vector.op("@@")(ts_query), Job.normalized_title.op("%")(filters.query.lower())),
            )
        if filters.eligibility_status:
            conditions.append(func.coalesce(ProfileJob.eligibility_status, PENDING) == filters.eligibility_status)
        if filters.remote_policy:
            conditions.append(Job.remote_policy == filters.remote_policy)
        if filters.seniority:
            conditions.append(Job.seniority == filters.seniority)
        if filters.source_id:
            conditions.append(Job.source_id == filters.source_id)
        if filters.company_id:
            conditions.append(Job.company_id == filters.company_id)
        if filters.min_score is not None:
            conditions.append(ProfileJob.match_score >= filters.min_score)
        if not filters.include_closed:
            conditions.append(Job.closed_at.is_(None))
        if not filters.include_duplicates:
            conditions.append(Job.duplicate_of_id.is_(None))
        if conditions:
            statement = statement.where(and_(*conditions))
        return statement

    async def search(
        self, filters: JobFilters, *, profile_id: uuid.UUID, limit: int, offset: int
    ) -> tuple[list[JobView], int]:
        base = self._filtered(filters, profile_id)
        total_stmt = select(func.count()).select_from(base.with_only_columns(Job.id).order_by(None).subquery())
        total = int((await self._session.execute(total_stmt)).scalar_one())
        orderings: dict[SortKey, tuple[ColumnElement[Any], ...]] = {
            "score": (ProfileJob.match_score.desc().nulls_last(), Job.published_at.desc().nulls_last()),
            "published": (Job.published_at.desc().nulls_last(), Job.id.desc()),
            "discovered": (Job.first_seen_at.desc(), Job.id.desc()),
        }
        statement = base.order_by(*orderings[filters.sort]).limit(limit).offset(offset)
        rows = (await self._session.execute(statement)).unique().all()
        return [JobView(job=row[0], state=row[1]) for row in rows], total

    async def view(self, job_id: uuid.UUID, profile_id: uuid.UUID) -> ProfileJob | None:
        return await self._session.get(ProfileJob, (profile_id, job_id))

    async def count_by_status(self, profile_id: uuid.UUID) -> dict[str, int]:
        status = func.coalesce(ProfileJob.eligibility_status, PENDING)
        statement = (
            select(status, func.count(Job.id))
            .select_from(Job)
            .outerjoin(ProfileJob, and_(ProfileJob.job_id == Job.id, ProfileJob.profile_id == profile_id))
            .where(Job.closed_at.is_(None), Job.duplicate_of_id.is_(None))
            .group_by(status)
        )
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}

    async def score_histogram(self, profile_id: uuid.UUID) -> list[tuple[float, int]]:
        bucket = func.width_bucket(ProfileJob.match_score, 0, 1.0001, 10)
        statement = (
            select(bucket, func.count())
            .select_from(ProfileJob)
            .join(Job, Job.id == ProfileJob.job_id)
            .where(ProfileJob.profile_id == profile_id, ProfileJob.match_score.is_not(None), Job.closed_at.is_(None))
            .group_by(bucket)
            .order_by(bucket)
        )
        return [((int(row[0]) - 1) / 10, int(row[1])) for row in (await self._session.execute(statement)).all()]

    async def discovered_per_day(self, since: datetime) -> list[tuple[datetime, int]]:
        day = func.date_trunc("day", Job.first_seen_at)
        statement = select(day, func.count(Job.id)).where(Job.first_seen_at >= since).group_by(day).order_by(day)
        return [(row[0], int(row[1])) for row in (await self._session.execute(statement)).all()]

    async def find_duplicate(
        self, job: NormalizedJob, *, exclude_source_id: uuid.UUID, company_id: uuid.UUID
    ) -> Job | None:
        """Layer 2 (canonical URL) then layer 3 (fingerprint) then fuzzy title within company."""
        open_original = and_(Job.closed_at.is_(None), Job.duplicate_of_id.is_(None), Job.source_id != exclude_source_id)
        exact = (
            select(Job)
            .where(
                open_original,
                or_(Job.canonical_url == job.canonical_url, Job.fingerprint == job.fingerprint),
            )
            .limit(1)
        )
        found = (await self._session.execute(exact)).scalar_one_or_none()
        if found is not None:
            return found
        fuzzy = (
            select(Job)
            .where(
                open_original,
                Job.company_id == company_id,
                func.similarity(Job.normalized_title, job.normalized_title) >= TRIGRAM_DUPLICATE_THRESHOLD,
                func.coalesce(Job.normalized_location, "") == (job.normalized_location or ""),
            )
            .order_by(func.similarity(Job.normalized_title, job.normalized_title).desc())
            .limit(1)
        )
        return (await self._session.execute(fuzzy)).scalar_one_or_none()

    async def similar_titles(self, job: Job, *, limit: int = 5) -> Sequence[tuple[Job, float]]:
        similarity = func.similarity(Job.normalized_title, job.normalized_title)
        statement = (
            select(Job, similarity)
            .options(joinedload(Job.company))
            .where(Job.id != job.id, Job.normalized_title.op("%")(job.normalized_title))
            .order_by(similarity.desc())
            .limit(limit)
        )
        return [(row[0], float(row[1])) for row in (await self._session.execute(statement)).unique().all()]

    # ------------------------------------------------------------------ writes

    async def insert(
        self,
        *,
        source_id: uuid.UUID,
        company: Company,
        job: NormalizedJob,
        duplicate_of_id: uuid.UUID | None,
        now: datetime,
    ) -> Job:
        row = Job(
            source_id=source_id,
            company_id=company.id,
            external_id=job.external_id,
            canonical_url=job.canonical_url,
            title=job.title,
            normalized_title=job.normalized_title,
            location=job.location,
            normalized_location=job.normalized_location,
            department=job.department,
            employment_type=job.employment_type,
            remote_policy=job.remote_policy.value,
            seniority=job.seniority.value,
            description_text=job.description_text,
            description_html=job.description_html,
            published_at=job.published_at or now,
            first_seen_at=now,
            last_seen_at=now,
            content_hash=job.content_hash,
            fingerprint=job.fingerprint,
            duplicate_of_id=duplicate_of_id,
            workflow_state="duplicate" if duplicate_of_id else "discovered",
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def apply_update(self, row: Job, job: NormalizedJob, *, now: datetime) -> None:
        row.title = job.title
        row.normalized_title = job.normalized_title
        row.canonical_url = job.canonical_url
        row.location = job.location
        row.normalized_location = job.normalized_location
        row.department = job.department
        row.employment_type = job.employment_type
        row.remote_policy = job.remote_policy.value
        row.seniority = job.seniority.value
        row.description_text = job.description_text
        row.description_html = job.description_html
        row.content_hash = job.content_hash
        row.fingerprint = job.fingerprint
        row.version += 1
        row.last_seen_at = now
        row.closed_at = None
        row.workflow_state = "discovered"
        await self._session.flush()
        # New content invalidates every profile's verdict (runs in system scope during ingestion).
        await self._session.execute(
            update(ProfileJob)
            .where(ProfileJob.job_id == row.id)
            .values(eligibility_status=PENDING, state="discovered", match_score=None)
        )

    async def add_version(self, row: Job, *, snapshot_id: uuid.UUID | None) -> None:
        self._session.add(
            JobVersion(
                job_id=row.id,
                version=row.version,
                content_hash=row.content_hash,
                title=row.title,
                location=row.location,
                description_text=row.description_text,
                snapshot_id=snapshot_id,
            ),
        )
        await self._session.flush()

    async def add_snapshot(
        self,
        *,
        source_id: uuid.UUID,
        job_id: uuid.UUID | None,
        key: str,
        snapshot_hash: str,
        content_type: str,
        size_bytes: int,
        fetched_at: datetime,
    ) -> RawSnapshot:
        snapshot = RawSnapshot(
            source_id=source_id,
            job_id=job_id,
            snapshot_key=key,
            snapshot_hash=snapshot_hash,
            content_type=content_type,
            size_bytes=size_bytes,
            fetched_at=fetched_at,
        )
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def latest_snapshot(self, job_id: uuid.UUID) -> RawSnapshot | None:
        statement = (
            select(RawSnapshot).where(RawSnapshot.job_id == job_id).order_by(RawSnapshot.fetched_at.desc()).limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def versions(self, job_id: uuid.UUID) -> Sequence[JobVersion]:
        statement = select(JobVersion).where(JobVersion.job_id == job_id).order_by(JobVersion.version.desc())
        return (await self._session.execute(statement)).scalars().all()

    async def touch_seen(self, source_id: uuid.UUID, external_ids: Sequence[str], now: datetime) -> None:
        if not external_ids:
            return
        await self._session.execute(
            update(Job)
            .where(Job.source_id == source_id, Job.external_id.in_(external_ids))
            .values(last_seen_at=now, closed_at=None),
        )

    async def close_missing(self, source_id: uuid.UUID, seen_external_ids: Sequence[str], now: datetime) -> int:
        """Mark open jobs that disappeared from a full listing as closed."""
        statement = (
            update(Job)
            .where(Job.source_id == source_id, Job.closed_at.is_(None), Job.external_id.not_in(seen_external_ids))
            .values(closed_at=now, workflow_state="closed")
        )
        result = await self._session.execute(statement)
        return int(result.rowcount or 0)  # type: ignore[attr-defined]

    async def set_state(
        self,
        job_id: uuid.UUID,
        *,
        profile_id: uuid.UUID,
        workflow_state: str | None = None,
        eligibility_status: str | None = None,
        match_score: float | None = None,
        clear_score: bool = False,
    ) -> None:
        """Upsert one profile's state for a job (workspace_id comes from the transaction scope)."""
        values: dict[str, object] = {}
        if workflow_state is not None:
            values["state"] = workflow_state
        if eligibility_status is not None:
            values["eligibility_status"] = eligibility_status
        if match_score is not None:
            values["match_score"] = match_score
        if clear_score:
            values["match_score"] = None
        if not values:
            return
        statement = (
            insert(ProfileJob)
            .values(profile_id=profile_id, job_id=job_id, **values)
            .on_conflict_do_update(
                index_elements=[ProfileJob.profile_id, ProfileJob.job_id], set_={**values, "updated_at": func.now()}
            )
        )
        await self._session.execute(statement)

    async def set_embedding(self, job_id: uuid.UUID, vector: list[float], content_hash: str) -> None:
        await self._session.execute(
            update(Job).where(Job.id == job_id).values(embedding=vector, embedding_hash=content_hash),
        )

    async def get_embedding(self, job_id: uuid.UUID) -> tuple[list[float] | None, str | None]:
        statement = select(Job.embedding, Job.embedding_hash).where(Job.id == job_id)
        row = (await self._session.execute(statement)).one_or_none()
        if row is None or row[0] is None:
            return None, None
        return [float(x) for x in row[0]], row[1]
