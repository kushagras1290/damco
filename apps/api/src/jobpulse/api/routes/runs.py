"""/api/v1/runs - workflow run history."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter

from jobpulse.api.deps import Paging, Session
from jobpulse.api.schemas import Page, RunOut
from jobpulse.core.errors import NotFoundError
from jobpulse.core.security import Reader
from jobpulse.repositories.activity import WorkflowRunRepository

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])

WorkflowType = Literal["SourceDiscoveryWorkflow", "JobEvaluationWorkflow"]
RunStatus = Literal["running", "completed", "failed", "cancelled"]


@router.get("", response_model=Page[RunOut])
async def list_runs(
    _: Reader,
    session: Session,
    paging: Paging,
    workflow_type: WorkflowType | None = None,
    status: RunStatus | None = None,
    source_id: uuid.UUID | None = None,
) -> Page[RunOut]:
    rows, total = await WorkflowRunRepository(session).list(
        workflow_type=workflow_type,
        status=status,
        source_id=source_id,
        limit=paging.limit,
        offset=paging.offset,
    )
    return Page(items=[RunOut.model_validate(r) for r in rows], total=total, limit=paging.limit, offset=paging.offset)


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, _: Reader, session: Session) -> RunOut:
    run = await WorkflowRunRepository(session).get(run_id)
    if run is None:
        raise NotFoundError("run not found")
    return RunOut.model_validate(run)
