"""ORM -> API schema mapping."""

from __future__ import annotations

from jobpulse.api.schemas import (
    ApplicationOut,
    JobSummary,
    ProfileOut,
    SourceOut,
)
from jobpulse.db.models import Application, Job, Profile, Source
from jobpulse_core.domain.models import EligibilityPolicy, Seniority


def job_summary(job: Job) -> JobSummary:
    return JobSummary(
        id=job.id,
        title=job.title,
        company=job.company.name,
        company_domain=job.company.domain,
        source_id=job.source_id,
        source_name=job.source.name,
        source_kind=job.source.kind,
        location=job.location,
        remote_policy=job.remote_policy,
        seniority=job.seniority,
        url=job.canonical_url,
        published_at=job.published_at,
        first_seen_at=job.first_seen_at,
        eligibility_status=job.eligibility_status,
        workflow_state=job.workflow_state,
        match_score=job.match_score,
        closed=job.closed_at is not None,
    )


def source_out(source: Source, open_jobs: int) -> SourceOut:
    return SourceOut(
        id=source.id,
        name=source.name,
        kind=source.kind,
        company=source.company.name,
        company_domain=source.company.domain,
        config=source.config,
        enabled=source.enabled,
        poll_interval_seconds=source.poll_interval_seconds,
        min_poll_interval_seconds=source.min_poll_interval_seconds,
        max_poll_interval_seconds=source.max_poll_interval_seconds,
        consecutive_failures=source.consecutive_failures,
        circuit_open_until=source.circuit_open_until,
        last_polled_at=source.last_polled_at,
        last_success_at=source.last_success_at,
        last_new_job_at=source.last_new_job_at,
        last_error=source.last_error,
        open_jobs=open_jobs,
        created_at=source.created_at,
    )


def profile_out(profile: Profile) -> ProfileOut:
    return ProfileOut(
        id=profile.id,
        display_name=profile.display_name,
        target_roles=list(profile.target_roles or []),
        skills=list(profile.skills or []),
        years_experience=float(profile.years_experience or 0),
        seniority=Seniority(profile.seniority),
        home_country=profile.home_country,
        timezone=profile.timezone,
        summary=profile.summary or "",
        policy=EligibilityPolicy.model_validate(profile.policy) if profile.policy else EligibilityPolicy(),
        notification_email=profile.notification_email,
        webhook_url=profile.webhook_url,
        notify_min_score=profile.notify_min_score,
        notifications_enabled=profile.notifications_enabled,
        has_embedding=profile.embedding is not None,
        updated_at=profile.updated_at,
    )


def application_out(application: Application) -> ApplicationOut:
    return ApplicationOut(
        id=application.id,
        job_id=application.job_id,
        job_title=application.job.title,
        company=application.job.company.name,
        status=application.status,
        notes=application.notes,
        applied_at=application.applied_at,
        created_at=application.created_at,
        updated_at=application.updated_at,
    )
