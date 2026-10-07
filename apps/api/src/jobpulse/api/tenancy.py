"""Request-scoped tenancy: who is calling, which workspace they act in, and their role.

Each request runs in one transaction: the account is resolved in system scope (identity,
memberships, provisioning), then the same transaction is narrowed to the chosen workspace's
row-level-security scope for everything the route does. Roles come from memberships.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.api.deps import Ctx
from jobpulse.core.security import Principal, current_principal
from jobpulse.db.models import Profile
from jobpulse.db.session import TENANT_ROLE_KEY, transaction
from jobpulse.db.tenancy import NO_SCOPE, SYSTEM_SCOPE, apply_scope, workspace_scope
from jobpulse.repositories.profiles import ProfileRepository
from jobpulse.services.accounts import Account, WorkspaceRole, resolve_account

ACCOUNT_STATE = "account"


async def get_session(
    request: Request, ctx: Ctx, principal: Annotated[Principal, Depends(current_principal)]
) -> AsyncIterator[AsyncSession]:
    """One transaction per request (commit on success), scoped to the caller's workspace."""
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        account = await resolve_account(session, principal, ctx.settings)
        request.state.account = account
        scope = workspace_scope(account.workspace.id) if account.workspace else NO_SCOPE
        request.state.workspace_id = account.workspace.id if account.workspace else None
        await apply_scope(session, scope, role=session.info.get(TENANT_ROLE_KEY))
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]


async def get_account(request: Request, _: Session) -> Account:
    account: Account = getattr(request.state, ACCOUNT_STATE)
    return account


CurrentAccount = Annotated[Account, Depends(get_account)]


def require_role(role: WorkspaceRole) -> Callable[[Account], Awaitable[Account]]:
    async def dependency(account: CurrentAccount) -> Account:
        account.require(role)
        return account

    return dependency


Viewer = Annotated[Account, Depends(require_role(WorkspaceRole.VIEWER))]
Member = Annotated[Account, Depends(require_role(WorkspaceRole.MEMBER))]
Admin = Annotated[Account, Depends(require_role(WorkspaceRole.ADMIN))]
WorkspaceOwner = Annotated[Account, Depends(require_role(WorkspaceRole.OWNER))]


async def get_workspace_id(account: Viewer) -> uuid.UUID:
    return account.require(WorkspaceRole.VIEWER).id


WorkspaceId = Annotated[uuid.UUID, Depends(get_workspace_id)]


async def get_active_profile(session: Session, _: Viewer) -> Profile:
    """The profile this request reads and writes (the workspace's primary profile)."""
    return await ProfileRepository(session).get_or_create_primary()


ActiveProfile = Annotated[Profile, Depends(get_active_profile)]


@asynccontextmanager
async def system_section(session: AsyncSession, account: Account) -> AsyncIterator[AsyncSession]:
    """Temporarily widen the request transaction to system scope for account-level writes
    (creating a workspace, accepting an invitation), then narrow it back."""
    role = session.info.get(TENANT_ROLE_KEY)
    await apply_scope(session, SYSTEM_SCOPE, role=role)
    yield session
    # Not in `finally`: after an error the transaction is aborted and rolls back anyway.
    scope = workspace_scope(account.workspace.id) if account.workspace else NO_SCOPE
    await apply_scope(session, scope, role=role)
