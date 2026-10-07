"""Append-only decision records: eligibility, AI intelligence, match scores, notifications."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import column, func, select, true
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from jobpulse.db.models import (
    EligibilityDecision,
    Job,
    JobIntelligenceRecord,
    MatchScore,
    Notification,
)


class DecisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------- eligibility

    async def add_eligibility(
        self,
        *,
        job_id: uuid.UUID,
        profile_id: uuid.UUID,
        stage: str,
        status: str,
        rules: list[dict[str, Any]],
        unresolved: list[str],
        policy_hash: str,
        content_hash: str,
        workflow_id: str | None,
    ) -> EligibilityDecision:
        decision = EligibilityDecision(
            job_id=job_id,
            profile_id=profile_id,
            stage=stage,
            status=status,
            rules=rules,
            unresolved=unresolved,
            policy_hash=policy_hash,
            content_hash=content_hash,
            workflow_id=workflow_id,
        )
        self._session.add(decision)
        await self._session.flush()
        return decision

    async def eligibility_history(self, job_id: uuid.UUID) -> Sequence[EligibilityDecision]:
        statement = (
            select(EligibilityDecision)
            .where(EligibilityDecision.job_id == job_id)
            .order_by(EligibilityDecision.created_at.desc())
        )
        return (await self._session.execute(statement)).scalars().all()

    async def latest_eligibility(self, job_id: uuid.UUID, *, stage: str | None = None) -> EligibilityDecision | None:
        statement = select(EligibilityDecision).where(EligibilityDecision.job_id == job_id)
        if stage is not None:
            statement = statement.where(EligibilityDecision.stage == stage)
        statement = statement.order_by(EligibilityDecision.created_at.desc()).limit(1)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def recent_decisions(
        self,
        *,
        status: str | None,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[tuple[EligibilityDecision, Job]], int]:
        base = select(EligibilityDecision, Job).join(Job, Job.id == EligibilityDecision.job_id)
        count_base = select(func.count(EligibilityDecision.id))
        if status:
            base = base.where(EligibilityDecision.status == status)
            count_base = count_base.where(EligibilityDecision.status == status)
        total = int((await self._session.execute(count_base)).scalar_one())
        statement = (
            base.options(joinedload(Job.company))
            .order_by(EligibilityDecision.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(statement)).unique().all()
        return [(row[0], row[1]) for row in rows], total

    async def rejection_reasons(self, since: datetime, *, limit: int = 10) -> list[tuple[str, int]]:
        """Most common failing rules among ineligible decisions (for the dashboard)."""
        element = (
            func.jsonb_array_elements(EligibilityDecision.rules).table_valued(column("value", JSONB)).alias("element")
        )
        rule_name = element.c.value["rule"].astext
        statement = (
            select(rule_name.label("rule_name"), func.count().label("n"))
            .select_from(EligibilityDecision)
            .join(element, true())
            .where(
                EligibilityDecision.created_at >= since,
                EligibilityDecision.status == "ineligible",
                element.c.value["outcome"].astext == "fail",
            )
            .group_by(rule_name)
            .order_by(func.count().desc())
            .limit(limit)
        )
        return [(str(row[0]), int(row[1])) for row in (await self._session.execute(statement)).all()]

    # ------------------------------------------------------------- intelligence

    async def get_intelligence(self, job_id: uuid.UUID, content_hash: str) -> JobIntelligenceRecord | None:
        statement = select(JobIntelligenceRecord).where(
            JobIntelligenceRecord.job_id == job_id,
            JobIntelligenceRecord.content_hash == content_hash,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def latest_intelligence(self, job_id: uuid.UUID) -> JobIntelligenceRecord | None:
        statement = (
            select(JobIntelligenceRecord)
            .where(JobIntelligenceRecord.job_id == job_id)
            .order_by(JobIntelligenceRecord.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def upsert_intelligence(
        self,
        *,
        job_id: uuid.UUID,
        content_hash: str,
        model: str,
        data: dict[str, Any],
        confidence: float,
        escalated: bool,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
    ) -> None:
        statement = (
            insert(JobIntelligenceRecord)
            .values(
                job_id=job_id,
                content_hash=content_hash,
                model=model,
                data=data,
                confidence=confidence,
                escalated=escalated,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=Decimal(str(cost_usd)),
            )
            .on_conflict_do_nothing(constraint="uq_job_intelligence_job_hash")
        )
        await self._session.execute(statement)

    async def total_llm_cost(self) -> Decimal:
        statement = select(func.coalesce(func.sum(JobIntelligenceRecord.cost_usd), 0))
        return Decimal((await self._session.execute(statement)).scalar_one())

    # ------------------------------------------------------------- scores

    async def add_score(
        self,
        *,
        job_id: uuid.UUID,
        profile_id: uuid.UUID,
        final_score: float,
        actionable: bool,
        components: list[dict[str, Any]],
        weights: dict[str, Any],
        matched_skills: list[str],
        missing_skills: list[str],
        content_hash: str,
        workflow_id: str | None,
    ) -> MatchScore:
        score = MatchScore(
            job_id=job_id,
            profile_id=profile_id,
            final_score=final_score,
            actionable=actionable,
            components=components,
            weights=weights,
            matched_skills=matched_skills,
            missing_skills=missing_skills,
            content_hash=content_hash,
            workflow_id=workflow_id,
        )
        self._session.add(score)
        await self._session.flush()
        return score

    async def latest_score(self, job_id: uuid.UUID) -> MatchScore | None:
        statement = (
            select(MatchScore).where(MatchScore.job_id == job_id).order_by(MatchScore.created_at.desc()).limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def score_histogram(self) -> list[tuple[float, int]]:
        bucket = func.width_bucket(Job.match_score, 0, 1.0001, 10)
        statement = (
            select(bucket, func.count(Job.id))
            .where(Job.match_score.is_not(None), Job.closed_at.is_(None))
            .group_by(bucket)
            .order_by(bucket)
        )
        return [((int(row[0]) - 1) / 10, int(row[1])) for row in (await self._session.execute(statement)).all()]

    # ------------------------------------------------------------- notifications

    async def claim_notification(
        self,
        *,
        job_id: uuid.UUID,
        profile_id: uuid.UUID,
        channel: str,
        dedupe_key: str,
    ) -> Notification | None:
        """Insert a pending notification; return None if it was already sent (idempotency)."""
        statement = (
            insert(Notification)
            .values(job_id=job_id, profile_id=profile_id, channel=channel, dedupe_key=dedupe_key)
            .on_conflict_do_nothing(index_elements=[Notification.dedupe_key])
        )
        await self._session.execute(statement)
        row = (
            await self._session.execute(
                select(Notification).where(Notification.dedupe_key == dedupe_key).with_for_update(),
            )
        ).scalar_one()
        if row.status in {"sent", "skipped"}:
            return None
        return row

    async def mark_notification(
        self, notification: Notification, *, status: str, error: str | None, now: datetime
    ) -> None:
        notification.status = status
        notification.attempts += 1
        notification.error = error[:2000] if error else None
        if status == "sent":
            notification.sent_at = now
        await self._session.flush()

    async def notifications_for_job(self, job_id: uuid.UUID) -> Sequence[Notification]:
        statement = select(Notification).where(Notification.job_id == job_id).order_by(Notification.created_at.desc())
        return (await self._session.execute(statement)).scalars().all()

    async def notification_counts(self) -> dict[str, int]:
        statement = select(Notification.status, func.count(Notification.id)).group_by(Notification.status)
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}
