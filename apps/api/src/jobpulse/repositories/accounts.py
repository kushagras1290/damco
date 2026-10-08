"""Users, sign-in identities, memberships and invitations.

Identity and cross-workspace membership lookups run in system scope (sign-in resolution);
member and invitation management runs in the workspace's scope.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import ColumnElement, case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from jobpulse.db.models import Identity, Invitation, Membership, User, Workspace

ROLE_RANK = {"member": 1, "admin": 2, "owner": 3}


def _rank(role: ColumnElement[str] | InstrumentedAttribute[str]) -> ColumnElement[int]:
    return case(ROLE_RANK, value=role, else_=0)


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    workspace: Workspace
    role: str


@dataclass(frozen=True, slots=True)
class MemberRow:
    user: User
    role: str
    joined_at: datetime


class AccountRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ identities (system scope)

    async def user_for_identity(self, provider: str, subject: str) -> User | None:
        statement = (
            select(User)
            .join(Identity, Identity.user_id == User.id)
            .where(Identity.provider == provider, Identity.subject == subject)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def create_user(self, *, provider: str, subject: str, display_name: str) -> User:
        user = User(display_name=display_name)
        self._session.add(user)
        await self._session.flush()
        # A concurrent first request may have created the identity: keep exactly one.
        inserted = await self._session.execute(
            insert(Identity)
            .values(provider=provider, subject=subject, user_id=user.id)
            .on_conflict_do_nothing()
            .returning(Identity.user_id)
        )
        if inserted.scalar_one_or_none() is None:
            await self._session.delete(user)
            existing = await self.user_for_identity(provider, subject)
            if existing is None:
                msg = "identity vanished during sign-up"
                raise LookupError(msg)
            return existing
        return user

    async def memberships(self, user_id: uuid.UUID) -> list[WorkspaceMembership]:
        """All workspaces of a user, personal first then oldest first (system scope)."""
        statement = (
            select(Workspace, Membership.role)
            .join(Membership, Membership.workspace_id == Workspace.id)
            .where(Membership.user_id == user_id)
            .order_by(Workspace.personal.desc(), Membership.created_at.asc())
        )
        return [WorkspaceMembership(row[0], row[1]) for row in (await self._session.execute(statement)).all()]

    async def add_membership(self, *, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
        """Insert, or upgrade an existing membership to ``role`` if that is higher."""
        statement = (
            insert(Membership)
            .values(workspace_id=workspace_id, user_id=user_id, role=role)
            .on_conflict_do_update(
                index_elements=[Membership.workspace_id, Membership.user_id],
                set_={"role": role},
                where=_rank(Membership.role) < ROLE_RANK[role],
            )
        )
        await self._session.execute(statement)

    async def create_workspace(self, *, name: str, slug: str, personal: bool, plan: str) -> Workspace:
        workspace = Workspace(name=name, slug=slug, personal=personal, plan=plan)
        self._session.add(workspace)
        await self._session.flush()
        return workspace

    # ------------------------------------------------------------------ members (workspace scope)

    async def members(self) -> list[MemberRow]:
        statement = (
            select(User, Membership.role, Membership.created_at)
            .join(Membership, Membership.user_id == User.id)
            .order_by(Membership.created_at.asc())
        )
        return [MemberRow(row[0], row[1], row[2]) for row in (await self._session.execute(statement)).all()]

    async def membership(self, user_id: uuid.UUID) -> Membership | None:
        statement = select(Membership).where(Membership.user_id == user_id).with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def owner_count(self) -> int:
        statement = select(func.count()).select_from(Membership).where(Membership.role == "owner")
        return int((await self._session.execute(statement)).scalar_one())

    async def workspace(self, workspace_id: uuid.UUID) -> Workspace | None:
        return await self._session.get(Workspace, workspace_id)

    async def is_member(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        statement = select(Membership.user_id).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
        return (await self._session.execute(statement)).first() is not None

    async def member_count_in(self, workspace_id: uuid.UUID) -> int:
        """System scope: members of a specific workspace."""
        statement = select(func.count()).select_from(Membership).where(Membership.workspace_id == workspace_id)
        return int((await self._session.execute(statement)).scalar_one())

    async def member_count(self) -> int:
        return int((await self._session.execute(select(func.count()).select_from(Membership))).scalar_one())

    async def remove_membership(self, membership: Membership) -> None:
        await self._session.delete(membership)
        await self._session.flush()

    # ------------------------------------------------------------------ invitations

    async def create_invitation(
        self, *, email: str | None, role: str, token_hash: str, invited_by: uuid.UUID | None, expires_at: datetime
    ) -> Invitation:
        invitation = Invitation(
            email=email, role=role, token_hash=token_hash, invited_by=invited_by, expires_at=expires_at
        )
        self._session.add(invitation)
        await self._session.flush()
        return invitation

    async def pending_invitations(self, now: datetime) -> Sequence[Invitation]:
        statement = (
            select(Invitation)
            .where(Invitation.accepted_at.is_(None), Invitation.revoked_at.is_(None), Invitation.expires_at > now)
            .order_by(Invitation.created_at.desc())
        )
        return (await self._session.execute(statement)).scalars().all()

    async def invitation(self, invitation_id: uuid.UUID) -> Invitation | None:
        return await self._session.get(Invitation, invitation_id, with_for_update=True)

    async def invitation_by_token_hash(self, token_hash: str) -> Invitation | None:
        """System scope: the token is the only thing an invitee has."""
        statement = select(Invitation).where(Invitation.token_hash == token_hash).with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def mark_accepted(self, invitation: Invitation, *, user_id: uuid.UUID, now: datetime) -> None:
        await self._session.execute(
            update(Invitation).where(Invitation.id == invitation.id).values(accepted_at=now, accepted_by=user_id)
        )
