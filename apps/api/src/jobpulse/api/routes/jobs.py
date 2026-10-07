"""/api/v1/jobs - listing, explainable detail, raw snapshot, re-evaluation, applications."""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Query, status

from jobpulse.api.deps import Ctx, Paging, Temporal
from jobpulse.api.mappers import application_out, job_summary
from jobpulse.api.schemas import (
    ActionAccepted,
    ApplicationCreate,
    ApplicationOut,
    EligibilityOut,
    IntelligenceOut,
    JobDetail,
    JobSummary,
    NotificationOut,
    Page,
    ScoreOut,
    SimilarJobOut,
    SnapshotContent,
    SnapshotOut,
    VersionOut,
)
from jobpulse.api.tenancy import ActiveProfile, Session
from jobpulse.core.errors import NotFoundError
from jobpulse.core.security import Owner, Reader
from jobpulse.repositories.activity import ApplicationRepository, AuditRepository
from jobpulse.repositories.decisions import DecisionRepository
from jobpulse.repositories.jobs import JobFilters, JobRepository, JobView, SortKey
from jobpulse.services import temporal as temporal_service
from jobpulse_core.contracts import JobRef

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

MAX_SNAPSHOT_PREVIEW_CHARS = 200_000
EligibilityFilter = Literal["pending", "eligible", "ineligible"]
RemoteFilter = Literal["remote", "hybrid", "onsite", "unknown"]


@router.get("", response_model=Page[JobSummary])
async def list_jobs(
    _: Reader,
    session: Session,
    profile: ActiveProfile,
    paging: Paging,
    q: Annotated[str | None, Query(min_length=2, max_length=200)] = None,
    eligibility: EligibilityFilter | None = None,
    remote_policy: RemoteFilter | None = None,
    seniority: Annotated[str | None, Query(max_length=20)] = None,
    source_id: uuid.UUID | None = None,
    min_score: Annotated[float | None, Query(ge=0, le=1)] = None,
    include_closed: bool = False,
    sort: SortKey = "score",
) -> Page[JobSummary]:
    filters = JobFilters(
        query=q,
        eligibility_status=eligibility,
        remote_policy=remote_policy,
        seniority=seniority,
        source_id=source_id,
        min_score=min_score,
        include_closed=include_closed,
        sort=sort,
    )
    rows, total = await JobRepository(session).search(
        filters, profile_id=profile.id, limit=paging.limit, offset=paging.offset
    )
    return Page(items=[job_summary(row) for row in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{job_id}", response_model=JobDetail)
async def get_job(job_id: uuid.UUID, _: Reader, session: Session, profile: ActiveProfile) -> JobDetail:
    jobs = JobRepository(session)
    job = await jobs.get_detail(job_id)
    if job is None:
        raise NotFoundError("job not found")
    decisions = DecisionRepository(session)
    history = await decisions.eligibility_history(job_id, profile.id)
    intelligence = await decisions.latest_intelligence(job_id)
    score = await decisions.latest_score(job_id, profile.id)
    snapshot = await jobs.latest_snapshot(job_id)
    application = await ApplicationRepository(session).for_job(job_id, profile.id)
    similar = await jobs.similar_titles(job)
    application_payload = None
    if application is not None:
        loaded = await ApplicationRepository(session).get(application.id)
        application_payload = application_out(loaded) if loaded else None

    summary = job_summary(JobView(job=job, state=await jobs.view(job_id, profile.id)))
    return JobDetail(
        **summary.model_dump(),
        department=job.department,
        employment_type=job.employment_type,
        description_html=job.description_html,
        description_text=job.description_text,
        version=job.version,
        content_hash=job.content_hash,
        fingerprint=job.fingerprint,
        duplicate_of_id=job.duplicate_of_id,
        eligibility=EligibilityOut.model_validate(history[0]) if history else None,
        eligibility_history=[EligibilityOut.model_validate(item) for item in history],
        intelligence=IntelligenceOut.model_validate(intelligence) if intelligence else None,
        score=ScoreOut.model_validate(score) if score else None,
        snapshot=SnapshotOut.model_validate(snapshot) if snapshot else None,
        versions=[VersionOut.model_validate(v) for v in await jobs.versions(job_id)],
        notifications=[
            NotificationOut.model_validate(n) for n in await decisions.notifications_for_job(job_id, profile.id)
        ],
        application=application_payload,
        similar=[
            SimilarJobOut(id=other.id, title=other.title, company=other.company.name, similarity=round(sim, 3))
            for other, sim in similar
        ],
    )


@router.get("/{job_id}/snapshot", response_model=SnapshotContent)
async def get_snapshot(job_id: uuid.UUID, _: Reader, session: Session, ctx: Ctx) -> SnapshotContent:
    snapshot = await JobRepository(session).latest_snapshot(job_id)
    if snapshot is None:
        raise NotFoundError("no snapshot for job")
    raw = (await ctx.store.get(snapshot.snapshot_key)).decode("utf-8", errors="replace")
    truncated = len(raw) > MAX_SNAPSHOT_PREVIEW_CHARS
    return SnapshotContent(
        snapshot=SnapshotOut.model_validate(snapshot),
        content=raw[:MAX_SNAPSHOT_PREVIEW_CHARS],
        truncated=truncated,
    )


@router.post("/{job_id}/evaluate", response_model=ActionAccepted, status_code=status.HTTP_202_ACCEPTED)
async def rerun_job(
    job_id: uuid.UUID,
    principal: Owner,
    session: Session,
    profile: ActiveProfile,
    ctx: Ctx,
    client: Temporal,
) -> ActionAccepted:
    if await JobRepository(session).get(job_id) is None:
        raise NotFoundError("job not found")
    workflow_id = await temporal_service.rerun_job(
        client,
        ctx.settings,
        JobRef(job_id=str(job_id), workspace_id=str(profile.workspace_id), profile_id=str(profile.id), force=True),
        nonce=secrets.token_hex(6),
    )
    await AuditRepository(session).record(
        actor=principal.actor,
        action="job.rerun",
        entity_type="job",
        entity_id=str(job_id),
        payload={"workflow_id": workflow_id},
    )
    return ActionAccepted(workflow_id=workflow_id)


@router.post("/{job_id}/applications", response_model=ApplicationOut, status_code=status.HTTP_201_CREATED)
async def create_application(
    job_id: uuid.UUID,
    body: ApplicationCreate,
    principal: Owner,
    session: Session,
    profile: ActiveProfile,
) -> ApplicationOut:
    if await JobRepository(session).get(job_id) is None:
        raise NotFoundError("job not found")
    application = await ApplicationRepository(session).upsert(
        job_id=job_id,
        profile_id=profile.id,
        status=body.status,
        notes=body.notes,
        applied_at=body.applied_at,
    )
    await AuditRepository(session).record(
        actor=principal.actor,
        action="application.upsert",
        entity_type="job",
        entity_id=str(job_id),
        payload={"status": body.status},
    )
    return application_out(application)
