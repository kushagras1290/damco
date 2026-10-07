"""Workflow runs, applications and audit events."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from jobpulse.db.models import Application, AuditEvent, Job, WorkflowRun


class WorkflowRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def start(
        self,
        *,
        workflow_id: str,
        run_id: str,
        workflow_type: str,
        source_id: uuid.UUID | None,
        job_id: uuid.UUID | None,
    ) -> None:
        statement = (
            insert(WorkflowRun)
            .values(
                workflow_id=workflow_id,
                run_id=run_id,
                workflow_type=workflow_type,
                source_id=source_id,
                job_id=job_id,
                status="running",
            )
            .on_conflict_do_nothing(constraint="uq_workflow_runs_workflow_run")
        )
        await self._session.execute(statement)

    async def finish(
        self,
        *,
        workflow_id: str,
        run_id: str,
        status: str,
        stats: dict[str, Any],
        error: str | None,
        now: datetime,
    ) -> None:
        statement = select(WorkflowRun).where(WorkflowRun.workflow_id == workflow_id, WorkflowRun.run_id == run_id)
        run = (await self._session.execute(statement)).scalar_one_or_none()
        if run is None:
            return
        run.status = status
        run.stats = stats
        run.error = error[:4000] if error else None
        run.finished_at = now
        await self._session.flush()

    async def get(self, run_id: uuid.UUID) -> WorkflowRun | None:
        return await self._session.get(WorkflowRun, run_id)

    async def list(
        self,
        *,
        workflow_type: str | None,
        status: str | None,
        source_id: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> tuple[Sequence[WorkflowRun], int]:
        statement = select(WorkflowRun)
        count = select(func.count(WorkflowRun.id))
        for column, value in (
            (WorkflowRun.workflow_type, workflow_type),
            (WorkflowRun.status, status),
            (WorkflowRun.source_id, source_id),
        ):
            if value is not None:
                statement = statement.where(column == value)
                count = count.where(column == value)
        total = int((await self._session.execute(count)).scalar_one())
        rows = (
            (await self._session.execute(statement.order_by(WorkflowRun.started_at.desc()).limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        return rows, total

    async def status_counts(self, since: datetime) -> dict[str, int]:
        statement = (
            select(WorkflowRun.status, func.count(WorkflowRun.id))
            .where(WorkflowRun.started_at >= since)
            .group_by(WorkflowRun.status)
        )
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}


class ApplicationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(
        self,
        *,
        job_id: uuid.UUID,
        profile_id: uuid.UUID,
        status: str,
        notes: str,
        applied_at: datetime | None,
    ) -> Application:
        statement = (
            insert(Application)
            .values(job_id=job_id, profile_id=profile_id, status=status, notes=notes, applied_at=applied_at)
            .on_conflict_do_update(
                constraint="uq_applications_job_profile",
                set_={"status": status, "notes": notes, "applied_at": applied_at, "updated_at": func.now()},
            )
            .returning(Application.id)
        )
        application_id = (await self._session.execute(statement)).scalar_one()
        return await self.get(application_id)  # type: ignore[return-value]

    async def get(self, application_id: uuid.UUID) -> Application | None:
        statement = (
            select(Application)
            .options(joinedload(Application.job).joinedload(Job.company))
            .where(Application.id == application_id)
            .execution_options(populate_existing=True)
        )
        return (await self._session.execute(statement)).unique().scalar_one_or_none()

    async def for_job(self, job_id: uuid.UUID, profile_id: uuid.UUID) -> Application | None:
        statement = select(Application).where(Application.job_id == job_id, Application.profile_id == profile_id)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list(self, *, status: str | None, limit: int, offset: int) -> tuple[Sequence[Application], int]:
        statement = select(Application).options(joinedload(Application.job).joinedload(Job.company))
        count = select(func.count(Application.id))
        if status:
            statement = statement.where(Application.status == status)
            count = count.where(Application.status == status)
        total = int((await self._session.execute(count)).scalar_one())
        rows = (
            (await self._session.execute(statement.order_by(Application.updated_at.desc()).limit(limit).offset(offset)))
            .unique()
            .scalars()
            .all()
        )
        return rows, total


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str | None,
        payload: dict[str, Any] | None = None,
        workspace_id: uuid.UUID | None = None,
    ) -> None:
        """``workspace_id`` defaults to the transaction's workspace scope (pass it in system scope)."""
        event = AuditEvent(
            actor=actor, action=action, entity_type=entity_type, entity_id=entity_id, payload=payload or {}
        )
        if workspace_id is not None:
            event.workspace_id = workspace_id
        self._session.add(event)
        await self._session.flush()

    async def list(self, *, entity_type: str | None, entity_id: str | None, limit: int) -> Sequence[AuditEvent]:
        statement = select(AuditEvent)
        if entity_type:
            statement = statement.where(AuditEvent.entity_type == entity_type)
        if entity_id:
            statement = statement.where(AuditEvent.entity_id == entity_id)
        return (
            (await self._session.execute(statement.order_by(AuditEvent.created_at.desc()).limit(limit))).scalars().all()
        )
