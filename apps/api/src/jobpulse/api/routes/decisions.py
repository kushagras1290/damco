"""/api/v1/decisions - eligibility decision audit log."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter

from jobpulse.api.deps import Paging
from jobpulse.api.schemas import DecisionOut, Page, RuleOut
from jobpulse.api.tenancy import Session, Viewer
from jobpulse.repositories.decisions import DecisionRepository

router = APIRouter(prefix="/api/v1/decisions", tags=["decisions"])


@router.get("", response_model=Page[DecisionOut])
async def list_decisions(
    _: Viewer,
    session: Session,
    paging: Paging,
    status: Literal["eligible", "ineligible"] | None = None,
) -> Page[DecisionOut]:
    rows, total = await DecisionRepository(session).recent_decisions(
        status=status,
        limit=paging.limit,
        offset=paging.offset,
    )
    items = [
        DecisionOut(
            id=decision.id,
            job_id=job.id,
            job_title=job.title,
            company=job.company.name,
            stage=decision.stage,
            status=decision.status,
            failed_rules=[RuleOut.model_validate(rule) for rule in decision.rules if rule.get("outcome") == "fail"],
            unresolved=list(decision.unresolved),
            created_at=decision.created_at,
        )
        for decision, job in rows
    ]
    return Page(items=items, total=total, limit=paging.limit, offset=paging.offset)
