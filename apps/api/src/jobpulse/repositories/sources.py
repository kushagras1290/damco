"""Companies, sources and checkpoints."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.db.models import Company, Job, Source, SourceCheckpoint
from jobpulse_core.domain.models import SourceCheckpoint as CheckpointState
from jobpulse_core.domain.models import SourceDefinition
from jobpulse_core.sources.locator import source_locator


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_company(self, *, name: str, domain: str) -> Company:
        statement = (
            insert(Company)
            .values(name=name, domain=domain.lower())
            .on_conflict_do_update(index_elements=[Company.domain], set_={"name": name})
            .returning(Company)
        )
        result = await self._session.execute(statement)
        return result.scalar_one()

    async def create(
        self,
        *,
        company_id: uuid.UUID,
        name: str,
        definition: SourceDefinition,
        poll_interval_seconds: int,
        min_poll_interval_seconds: int,
        max_poll_interval_seconds: int,
    ) -> Source:
        source = Source(
            company_id=company_id,
            name=name,
            kind=definition.kind.value,
            locator=source_locator(definition),
            config=definition.model_dump(mode="json"),
            poll_interval_seconds=poll_interval_seconds,
            min_poll_interval_seconds=min_poll_interval_seconds,
            max_poll_interval_seconds=max_poll_interval_seconds,
        )
        self._session.add(source)
        await self._session.flush()
        await self._session.refresh(source, attribute_names=["company"])
        return source

    async def get(self, source_id: uuid.UUID, *, for_update: bool = False) -> Source | None:
        statement = select(Source).where(Source.id == source_id)
        if for_update:
            statement = statement.with_for_update(of=Source)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_board(self, definition: SourceDefinition, *, for_update: bool = False) -> Source | None:
        """The catalogue source for this board, if any workspace already added it."""
        statement = select(Source).where(
            Source.kind == definition.kind.value, Source.locator == source_locator(definition)
        )
        if for_update:
            statement = statement.with_for_update(of=Source)
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list(self, *, enabled: bool | None = None, limit: int = 100, offset: int = 0) -> Sequence[Source]:
        statement = select(Source).order_by(Source.created_at.desc()).limit(limit).offset(offset)
        if enabled is not None:
            statement = statement.where(Source.enabled.is_(enabled))
        return (await self._session.execute(statement)).scalars().all()

    async def count(self) -> int:
        return int((await self._session.execute(select(func.count(Source.id)))).scalar_one())

    async def job_counts(self, source_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
        if not source_ids:
            return {}
        statement = (
            select(Job.source_id, func.count(Job.id))
            .where(Job.source_id.in_(source_ids), Job.closed_at.is_(None))
            .group_by(Job.source_id)
        )
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}

    async def get_checkpoint(self, source_id: uuid.UUID) -> CheckpointState:
        row = await self._session.get(SourceCheckpoint, source_id)
        return CheckpointState.model_validate(row.state) if row is not None else CheckpointState()

    async def save_checkpoint(self, source_id: uuid.UUID, state: CheckpointState) -> None:
        payload = state.model_dump(mode="json")
        statement = (
            insert(SourceCheckpoint)
            .values(source_id=source_id, state=payload)
            .on_conflict_do_update(
                index_elements=[SourceCheckpoint.source_id],
                set_={"state": payload, "updated_at": func.now()},
            )
        )
        await self._session.execute(statement)

    async def record_poll(
        self,
        source: Source,
        *,
        now: datetime,
        success: bool,
        new_jobs: int,
        next_interval_seconds: int,
        error: str | None,
        circuit_open_until: datetime | None,
    ) -> None:
        source.last_polled_at = now
        source.poll_interval_seconds = next_interval_seconds
        source.circuit_open_until = circuit_open_until
        if success:
            source.consecutive_failures = 0
            source.last_success_at = now
            source.last_error = None
            if new_jobs > 0:
                source.last_new_job_at = now
        else:
            source.consecutive_failures += 1
            source.last_error = (error or "unknown error")[:2000]
        await self._session.flush()
