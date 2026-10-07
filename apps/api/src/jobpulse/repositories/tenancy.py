"""Workspaces, memberships and source subscriptions."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.db.models import Profile, Source, SourceSubscription, Workspace


class WorkspaceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, workspace_id: uuid.UUID) -> Workspace | None:
        return await self._session.get(Workspace, workspace_id)

    async def ensure(self, *, workspace_id: uuid.UUID, name: str, slug: str, plan: str) -> Workspace:
        """Idempotently create a workspace with a fixed id (system scope only)."""
        statement = (
            insert(Workspace)
            .values(id=workspace_id, name=name, slug=slug, plan=plan)
            .on_conflict_do_nothing(index_elements=[Workspace.id])
        )
        await self._session.execute(statement)
        workspace = await self.get(workspace_id)
        if workspace is None:
            msg = f"workspace {workspace_id} not visible after insert (missing system scope?)"
            raise LookupError(msg)
        return workspace


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def subscribe(self, source_id: uuid.UUID, *, workspace_id: uuid.UUID | None = None) -> None:
        """Follow a source. ``workspace_id`` defaults to the transaction's workspace scope."""
        values: dict[str, uuid.UUID] = {"source_id": source_id}
        if workspace_id is not None:
            values["workspace_id"] = workspace_id
        await self._session.execute(insert(SourceSubscription).values(**values).on_conflict_do_nothing())

    async def subscribed_sources(self, *, limit: int, offset: int) -> Sequence[Source]:
        """Sources the current workspace follows (RLS limits subscriptions to this workspace)."""
        statement = (
            select(Source)
            .join(SourceSubscription, SourceSubscription.source_id == Source.id)
            .order_by(Source.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def count(self) -> int:
        return int((await self._session.execute(select(func.count()).select_from(SourceSubscription))).scalar_one())

    async def is_subscribed(self, source_id: uuid.UUID) -> bool:
        statement = select(SourceSubscription.source_id).where(SourceSubscription.source_id == source_id).limit(1)
        return (await self._session.execute(statement)).first() is not None

    async def evaluation_targets(self, source_id: uuid.UUID) -> list[tuple[uuid.UUID, uuid.UUID]]:
        """(workspace_id, profile_id) for every profile whose workspace follows the source.

        Cross-tenant by design: call only in system scope (discovery fan-out).
        """
        statement = (
            select(Profile.workspace_id, Profile.id)
            .join(SourceSubscription, SourceSubscription.workspace_id == Profile.workspace_id)
            .where(SourceSubscription.source_id == source_id)
            .order_by(Profile.workspace_id, Profile.created_at)
        )
        return [(row[0], row[1]) for row in (await self._session.execute(statement)).all()]
