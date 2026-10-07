"""Request-scoped tenancy: which workspace (and profile) a request acts on.

The workspace is resolved from the verified principal, then every database transaction of
the request runs in that workspace's row-level-security scope.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.api.deps import Ctx
from jobpulse.core.security import Principal, current_principal
from jobpulse.db.models import DEFAULT_WORKSPACE_ID, Profile
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import workspace_scope
from jobpulse.repositories.profiles import ProfileRepository


async def get_workspace_id(request: Request, principal: Annotated[Principal, Depends(current_principal)]) -> uuid.UUID:
    """Single-workspace mode: everyone acts on the Default workspace.

    Multi-workspace sign-in (workspace claim + membership check) replaces this resolver.
    """
    del principal  # resolved first so unauthenticated requests fail before any DB work
    workspace_id = DEFAULT_WORKSPACE_ID
    request.state.workspace_id = workspace_id
    return workspace_id


WorkspaceId = Annotated[uuid.UUID, Depends(get_workspace_id)]


async def get_session(ctx: Ctx, workspace_id: WorkspaceId) -> AsyncIterator[AsyncSession]:
    """One transaction per request in the caller's workspace scope (commit on success)."""
    async with transaction(ctx.sessions, scope=workspace_scope(workspace_id)) as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def get_active_profile(session: Session) -> Profile:
    """The profile this request reads and writes (the workspace's primary profile)."""
    return await ProfileRepository(session).get_or_create_primary()


ActiveProfile = Annotated[Profile, Depends(get_active_profile)]
