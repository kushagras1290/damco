"""Job evaluation stages executed by JobEvaluationWorkflow activities.

Stage order: eligibility -> enrichment -> embedding -> ranking -> notification.
Each stage reads its inputs from PostgreSQL and persists its decision, so stages are
idempotent and every decision is replayable from the audit trail.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import structlog

from jobpulse.core.metrics import (
    JOB_PROCESSING_DURATION,
    JOBS_MATCHED,
    JOBS_REJECTED,
    LLM_BUDGET_EXHAUSTED,
    LLM_COST_USD,
    LLM_REQUESTS,
    NOTIFICATIONS_SENT,
)
from jobpulse.db.models import Company, Job, Profile
from jobpulse.db.session import transaction
from jobpulse.repositories.decisions import DecisionRepository
from jobpulse.repositories.jobs import JobRepository
from jobpulse.repositories.profiles import ProfileRepository, to_domain
from jobpulse.services.context import AppContext
from jobpulse_core.contracts import JobRef, StageResult
from jobpulse_core.domain.models import (
    EligibilityStatus,
    JobIntelligence,
    NormalizedJob,
    RemotePolicy,
    Seniority,
)
from jobpulse_core.eligibility import RuleName, RuleOutcome, RuleResult, RuleSource, apply_intelligence, evaluate
from jobpulse_core.eligibility.engine import EligibilityResult
from jobpulse_core.errors import IntelligenceError, JobPulseError, NotificationError
from jobpulse_core.ingestion.normalize import sha256_hex
from jobpulse_core.intelligence import build_job_embedding_text, build_profile_embedding_text
from jobpulse_core.notifications import (
    NotificationMessage,
    NotificationProvider,
    ResendEmailProvider,
    WebhookProvider,
)
from jobpulse_core.ranking import score_job

logger = structlog.get_logger(__name__)

STAGE_DETERMINISTIC = "deterministic"
STAGE_POST_ENRICHMENT = "post_enrichment"


class JobNotFoundError(JobPulseError):
    """Job vanished or changed; the evaluation must stop (non-retryable)."""


def job_to_domain(row: Job, company: Company) -> NormalizedJob:
    return NormalizedJob(
        external_id=row.external_id,
        title=row.title,
        normalized_title=row.normalized_title,
        canonical_url=row.canonical_url,
        company_name=company.name,
        company_domain=company.domain,
        location=row.location,
        normalized_location=row.normalized_location,
        department=row.department,
        employment_type=row.employment_type,
        description_html=row.description_html,
        description_text=row.description_text,
        published_at=row.published_at,
        remote_policy=RemotePolicy(row.remote_policy),
        seniority=Seniority(row.seniority),
        content_hash=row.content_hash,
        fingerprint=row.fingerprint,
        raw_hash=row.content_hash,
    )


def result_from_rules(status: str, rules: list[dict[str, str]], policy_hash: str) -> EligibilityResult:
    parsed = tuple(
        RuleResult(
            RuleName(r["rule"]), RuleOutcome(r["outcome"]), r["evidence"], RuleSource(r.get("source", "deterministic"))
        )
        for r in rules
    )
    unresolved = tuple(r.rule for r in parsed if r.outcome is RuleOutcome.UNKNOWN)
    return EligibilityResult(
        status=EligibilityStatus(status), rules=parsed, policy_hash=policy_hash, unresolved=unresolved
    )


class EvaluationService:
    def __init__(self, ctx: AppContext) -> None:
        self._ctx = ctx

    async def _load(self, session_jobs: JobRepository, ref: JobRef) -> Job:
        row = await session_jobs.get(uuid.UUID(ref.job_id))
        if row is None:
            raise JobNotFoundError("job not found", context={"job_id": ref.job_id})
        if ref.content_hash and row.content_hash != ref.content_hash and not ref.force:
            raise JobNotFoundError(
                "job content changed; superseded by newer evaluation", context={"job_id": ref.job_id}
            )
        return row

    # ------------------------------------------------------------------ 1. eligibility

    async def eligibility(self, ref: JobRef, workflow_id: str | None) -> StageResult:
        started = time.perf_counter()
        async with transaction(self._ctx.sessions) as session:
            jobs = JobRepository(session)
            row = await self._load(jobs, ref)
            company = await session.get(Company, row.company_id)
            profile = await ProfileRepository(session).get_or_create_primary()
            if company is None:
                raise JobNotFoundError("company missing", context={"job_id": ref.job_id})
            domain_profile = to_domain(profile)
            result = evaluate(job_to_domain(row, company), domain_profile.policy)
            await DecisionRepository(session).add_eligibility(
                job_id=row.id,
                profile_id=profile.id,
                stage=STAGE_DETERMINISTIC,
                status=result.status.value,
                rules=[r.to_dict() for r in result.rules],
                unresolved=[u.value for u in result.unresolved],
                policy_hash=result.policy_hash,
                content_hash=row.content_hash,
                workflow_id=workflow_id,
            )
            await jobs.set_state(
                row.id,
                eligibility_status=result.status.value,
                workflow_state="eligibility_checked" if result.eligible else "rejected",
                clear_score=not result.eligible,
            )
        if not result.eligible:
            for rule in result.rules:
                if rule.outcome is RuleOutcome.FAIL:
                    JOBS_REJECTED.labels(stage=STAGE_DETERMINISTIC, rule=rule.rule.value).inc()
        JOB_PROCESSING_DURATION.labels(stage="eligibility").observe(time.perf_counter() - started)
        logger.info(
            "job.eligibility",
            job_id=ref.job_id,
            workflow_id=workflow_id,
            eligible=result.eligible,
            unresolved=[u.value for u in result.unresolved],
        )
        return StageResult(
            job_id=ref.job_id,
            proceed=result.eligible,
            eligible=result.eligible,
            detail=",".join(u.value for u in result.unresolved),
        )

    # ------------------------------------------------------------------ 2. enrichment

    async def enrich(self, ref: JobRef, workflow_id: str | None) -> StageResult:
        intelligence = self._ctx.intelligence
        if intelligence is None:
            return StageResult(job_id=ref.job_id, proceed=True, eligible=True, detail="intelligence disabled")
        started = time.perf_counter()
        async with transaction(self._ctx.sessions) as session:
            jobs = JobRepository(session)
            row = await self._load(jobs, ref)
            company = await session.get(Company, row.company_id)
            decisions = DecisionRepository(session)
            cached = await decisions.get_intelligence(row.id, row.content_hash)
            deterministic = await decisions.latest_eligibility(row.id, stage=STAGE_DETERMINISTIC)
            if company is None or deterministic is None:
                raise JobNotFoundError("eligibility must run before enrichment", context={"job_id": ref.job_id})
            job = job_to_domain(row, company)

        if cached is not None:
            intel = JobIntelligence.model_validate(cached.data)
            model = cached.model
        else:
            exhausted = await self._llm_budget_exhausted()
            if exhausted is not None:
                # Degrade, never block: deterministic eligibility already stands.
                LLM_BUDGET_EXHAUSTED.labels(limit=exhausted).inc()
                logger.warning("intelligence.budget_exhausted", job_id=ref.job_id, limit=exhausted)
                return StageResult(job_id=ref.job_id, proceed=True, eligible=True, detail=f"llm {exhausted} reached")
            try:
                outcome = await intelligence.extract(job, deterministic.unresolved)
            except IntelligenceError as exc:
                LLM_REQUESTS.labels(
                    model=intelligence.config.classification_model, kind="extract", outcome=type(exc).__name__
                ).inc()
                raise
            intel, model = outcome.intelligence, outcome.model
            LLM_REQUESTS.labels(model=model, kind="extract", outcome="ok").inc()
            LLM_COST_USD.labels(model=model).inc(outcome.cost_usd)
            async with transaction(self._ctx.sessions) as session:
                await DecisionRepository(session).upsert_intelligence(
                    job_id=uuid.UUID(ref.job_id),
                    content_hash=job.content_hash,
                    model=model,
                    data=intel.model_dump(mode="json"),
                    confidence=intel.confidence,
                    escalated=outcome.escalated,
                    input_tokens=outcome.input_tokens,
                    output_tokens=outcome.output_tokens,
                    cost_usd=outcome.cost_usd,
                )

        async with transaction(self._ctx.sessions) as session:
            profile = await ProfileRepository(session).get_or_create_primary()
            policy = to_domain(profile).policy
            base = result_from_rules(deterministic.status, deterministic.rules, deterministic.policy_hash)
            merged = apply_intelligence(base, intel, policy)
            await DecisionRepository(session).add_eligibility(
                job_id=uuid.UUID(ref.job_id),
                profile_id=profile.id,
                stage=STAGE_POST_ENRICHMENT,
                status=merged.status.value,
                rules=[r.to_dict() for r in merged.rules],
                unresolved=[u.value for u in merged.unresolved],
                policy_hash=merged.policy_hash,
                content_hash=job.content_hash,
                workflow_id=workflow_id,
            )
            await JobRepository(session).set_state(
                uuid.UUID(ref.job_id),
                eligibility_status=merged.status.value,
                workflow_state="enriched" if merged.eligible else "rejected",
                clear_score=not merged.eligible,
            )
        if not merged.eligible:
            for rule in merged.rules:
                if rule.outcome is RuleOutcome.FAIL and rule.source is RuleSource.AI:
                    JOBS_REJECTED.labels(stage=STAGE_POST_ENRICHMENT, rule=rule.rule.value).inc()
        JOB_PROCESSING_DURATION.labels(stage="enrichment").observe(time.perf_counter() - started)
        logger.info(
            "job.enriched",
            job_id=ref.job_id,
            workflow_id=workflow_id,
            model=model,
            confidence=intel.confidence,
            eligible=merged.eligible,
        )
        return StageResult(job_id=ref.job_id, proceed=merged.eligible, eligible=merged.eligible, detail=model)

    async def _llm_budget_exhausted(self) -> str | None:
        """Name of the daily limit that is exhausted, or None if spending is allowed."""
        settings = self._ctx.settings
        start_of_day = datetime.now(tz=UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with transaction(self._ctx.sessions) as session:
            count, cost = await DecisionRepository(session).intelligence_usage_since(start_of_day)
        if count >= settings.openai_daily_request_limit:
            return "daily request limit"
        if settings.openai_daily_budget_usd is not None and cost >= Decimal(str(settings.openai_daily_budget_usd)):
            return "daily budget"
        return None

    # ------------------------------------------------------------------ 3. embeddings

    async def embed(self, ref: JobRef) -> StageResult:
        intelligence = self._ctx.intelligence
        if intelligence is None:
            return StageResult(job_id=ref.job_id, proceed=True, detail="intelligence disabled")
        started = time.perf_counter()
        async with transaction(self._ctx.sessions) as session:
            jobs = JobRepository(session)
            row = await self._load(jobs, ref)
            company = await session.get(Company, row.company_id)
            profile = await ProfileRepository(session).get_or_create_primary()
            if company is None:
                raise JobNotFoundError("company missing", context={"job_id": ref.job_id})
            job_text = build_job_embedding_text(job_to_domain(row, company))
            profile_text = build_profile_embedding_text(to_domain(profile))
            need_job = row.embedding_hash != sha256_hex(job_text)
            need_profile = profile.embedding_hash != sha256_hex(profile_text)
            profile_id = profile.id

        texts = [t for t, needed in ((job_text, need_job), (profile_text, need_profile)) if needed]
        if not texts:
            return StageResult(job_id=ref.job_id, proceed=True, detail="embeddings cached")
        outcome = await intelligence.embed(texts)
        LLM_REQUESTS.labels(model=outcome.model, kind="embed", outcome="ok").inc()
        LLM_COST_USD.labels(model=outcome.model).inc(outcome.cost_usd)
        vectors = iter(outcome.vectors)
        async with transaction(self._ctx.sessions) as session:
            if need_job:
                await JobRepository(session).set_embedding(uuid.UUID(ref.job_id), next(vectors), sha256_hex(job_text))
            if need_profile:
                profile_row = await session.get(Profile, profile_id, with_for_update=True)
                if profile_row is not None:
                    await ProfileRepository(session).set_embedding(
                        profile_row,
                        vector=next(vectors),
                        model=outcome.model,
                        content_hash=sha256_hex(profile_text),
                    )
        JOB_PROCESSING_DURATION.labels(stage="embedding").observe(time.perf_counter() - started)
        return StageResult(job_id=ref.job_id, proceed=True, detail=f"embedded {len(texts)}")

    # ------------------------------------------------------------------ 4. ranking

    async def rank(self, ref: JobRef, workflow_id: str | None) -> StageResult:
        started = time.perf_counter()
        async with transaction(self._ctx.sessions) as session:
            jobs = JobRepository(session)
            decisions = DecisionRepository(session)
            row = await self._load(jobs, ref)
            company = await session.get(Company, row.company_id)
            profile = await ProfileRepository(session).get_or_create_primary()
            latest = await decisions.latest_eligibility(row.id)
            if company is None or latest is None:
                raise JobNotFoundError("eligibility must run before ranking", context={"job_id": ref.job_id})
            intel_row = await decisions.get_intelligence(row.id, row.content_hash)
            intel = JobIntelligence.model_validate(intel_row.data) if intel_row else None
            job_vector, _ = await jobs.get_embedding(row.id)
            profile_vector = [float(x) for x in profile.embedding] if profile.embedding is not None else None
            eligibility = result_from_rules(latest.status, latest.rules, latest.policy_hash)
            match = score_job(
                profile=to_domain(profile),
                job=job_to_domain(row, company),
                eligibility=eligibility,
                intelligence=intel,
                profile_embedding=profile_vector,
                job_embedding=job_vector,
            )
            await decisions.add_score(
                job_id=row.id,
                profile_id=profile.id,
                final_score=match.final_score,
                actionable=match.actionable,
                components=[c.to_dict() for c in match.components],
                weights=match.effective_weights,
                matched_skills=match.matched_skills,
                missing_skills=match.missing_skills,
                content_hash=row.content_hash,
                workflow_id=workflow_id,
            )
            await jobs.set_state(
                row.id,
                workflow_state="ranked" if match.actionable else "rejected",
                match_score=match.final_score if match.actionable else None,
                clear_score=not match.actionable,
            )
            threshold = profile.notify_min_score
        if match.actionable and match.final_score >= threshold:
            JOBS_MATCHED.inc()
        JOB_PROCESSING_DURATION.labels(stage="ranking").observe(time.perf_counter() - started)
        logger.info(
            "job.evaluated",
            job_id=ref.job_id,
            workflow_id=workflow_id,
            eligible=match.actionable,
            score=match.final_score,
        )
        return StageResult(
            job_id=ref.job_id,
            proceed=match.actionable and match.final_score >= threshold,
            eligible=match.actionable,
            score=match.final_score,
        )

    # ------------------------------------------------------------------ 5. notification

    def _providers(self, profile: Profile) -> list[NotificationProvider]:
        settings = self._ctx.settings
        providers: list[NotificationProvider] = []
        if profile.notification_email and settings.resend_api_key and settings.notification_from_email:
            providers.append(
                ResendEmailProvider(
                    api_key=settings.resend_api_key.get_secret_value(),
                    sender=settings.notification_from_email,
                    recipient=profile.notification_email,
                    http=self._ctx.notify_http,
                ),
            )
        if profile.webhook_url and settings.webhook_signing_secret:
            providers.append(
                WebhookProvider(
                    url=profile.webhook_url,
                    secret=settings.webhook_signing_secret.get_secret_value(),
                    http=self._ctx.notify_http,
                ),
            )
        return providers

    async def notify(self, ref: JobRef) -> StageResult:
        async with transaction(self._ctx.sessions) as session:
            jobs = JobRepository(session)
            row = await self._load(jobs, ref)
            company = await session.get(Company, row.company_id)
            profile = await ProfileRepository(session).get_or_create_primary()
            score = await DecisionRepository(session).latest_score(row.id)
            if company is None or score is None or not score.actionable:
                return StageResult(job_id=ref.job_id, proceed=False, detail="not actionable")
            if not profile.notifications_enabled or score.final_score < profile.notify_min_score:
                return StageResult(job_id=ref.job_id, proceed=False, detail="below threshold or disabled")
            providers = self._providers(profile)
            reasons = [f"{c['name']}: {c['detail']}" for c in score.components if c.get("value") is not None]
            base_message = NotificationMessage(
                dedupe_key="",
                job_id=str(row.id),
                title=row.title,
                company=company.name,
                url=row.canonical_url,
                location=row.location,
                score=score.final_score,
                matched_skills=list(score.matched_skills),
                missing_skills=list(score.missing_skills),
                reasons=reasons,
                dashboard_url=f"{self._ctx.settings.dashboard_base_url.rstrip('/')}/jobs/{row.id}",
            )
            profile_id, content_hash = profile.id, row.content_hash

        sent = 0
        for provider in providers:
            dedupe_key = f"{ref.job_id}:{content_hash}:{provider.channel}"
            async with transaction(self._ctx.sessions) as session:
                decisions = DecisionRepository(session)
                claim = await decisions.claim_notification(
                    job_id=uuid.UUID(ref.job_id),
                    profile_id=profile_id,
                    channel=provider.channel,
                    dedupe_key=dedupe_key,
                )
                if claim is None:
                    continue
                try:
                    await provider.send(replace(base_message, dedupe_key=dedupe_key))
                except NotificationError as exc:
                    await decisions.mark_notification(
                        claim, status="failed", error=exc.message, now=datetime.now(tz=UTC)
                    )
                    NOTIFICATIONS_SENT.labels(channel=provider.channel, outcome="failed").inc()
                    if exc.retryable:
                        raise
                    continue
                await decisions.mark_notification(claim, status="sent", error=None, now=datetime.now(tz=UTC))
            NOTIFICATIONS_SENT.labels(channel=provider.channel, outcome="sent").inc()
            sent += 1

        async with transaction(self._ctx.sessions) as session:
            await JobRepository(session).set_state(
                uuid.UUID(ref.job_id), workflow_state="notified" if sent else "ranked"
            )
        return StageResult(job_id=ref.job_id, proceed=sent > 0, detail=f"sent {sent}")
