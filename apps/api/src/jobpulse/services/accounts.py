"""Who is calling, and as which workspace member.

``resolve_account`` turns a verified principal into an :class:`Account`: it looks up (or,
per ``SIGNUP_POLICY``, provisions) the user behind the sign-in identity, bootstraps platform
admins into the Default workspace, and picks the workspace for this request - the one the
caller selected if they are a member, otherwise their personal / oldest workspace. Roles
always come from the database, never from the token.

Runs in system scope (it must see memberships across workspaces); callers then narrow the
transaction to the chosen workspace.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import IntEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from jobpulse.core.config import Settings
from jobpulse.core.errors import AuthenticationError, ConflictError, NotFoundError, PermissionDeniedError
from jobpulse.core.security import Principal
from jobpulse.db.models import DEFAULT_WORKSPACE_ID, Profile, User, Workspace
from jobpulse.repositories.accounts import AccountRepository, WorkspaceMembership
from jobpulse.services.plans import effective_plan, limits_for, require_capacity
from jobpulse_core.domain.models import EligibilityPolicy

logger = structlog.get_logger(__name__)

INVITATION_TTL = timedelta(days=7)
INVITATION_TOKEN_BYTES = 32
SLUG_SUFFIX_BYTES = 3
MAX_SLUG_BASE = 40
SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


class WorkspaceRole(IntEnum):
    """Ordered: a higher role can do everything a lower one can."""

    VIEWER = 0  # anonymous / signed visitor on the public demo workspace (read-only)
    MEMBER = 1  # profile, applications, re-evaluations
    ADMIN = 2  # + sources, members, invitations
    OWNER = 3  # + the workspace itself; only owners grant or remove owner

    @classmethod
    def parse(cls, value: str) -> WorkspaceRole:
        return cls[value.upper()]

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    id: uuid.UUID
    name: str
    slug: str
    plan: str
    personal: bool
    role: WorkspaceRole


@dataclass(frozen=True, slots=True)
class Account:
    principal: Principal
    user_id: uuid.UUID | None
    display_name: str
    workspace: WorkspaceSummary | None
    workspaces: list[WorkspaceSummary] = field(default_factory=list)

    @property
    def role(self) -> WorkspaceRole | None:
        return self.workspace.role if self.workspace else None

    def require(self, role: WorkspaceRole) -> WorkspaceSummary:
        if self.workspace is None:
            raise PermissionDeniedError("join or create a workspace first")
        if self.workspace.role < role:
            raise PermissionDeniedError(f"{role.label} role required in this workspace")
        return self.workspace


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_slug(name: str) -> str:
    base = SLUG_STRIP_RE.sub("-", name.lower()).strip("-")[:MAX_SLUG_BASE] or "workspace"
    return f"{base}-{secrets.token_hex(SLUG_SUFFIX_BYTES)}"


def _summary(membership: WorkspaceMembership) -> WorkspaceSummary:
    ws = membership.workspace
    return WorkspaceSummary(
        id=ws.id,
        name=ws.name,
        slug=ws.slug,
        plan=effective_plan(
            ws.plan, subscription_status=ws.subscription_status, grace_until=ws.grace_until, now=datetime.now(tz=UTC)
        ).value,
        personal=ws.personal,
        role=WorkspaceRole.parse(membership.role),
    )


def _viewer_of_demo(principal: Principal, settings: Settings, workspaces: list[WorkspaceSummary]) -> Account:
    if not settings.public_demo_enabled:
        raise AuthenticationError("authentication required")
    demo = WorkspaceSummary(
        id=DEFAULT_WORKSPACE_ID,
        name="Public demo",
        slug="default",
        plan="team",
        personal=False,
        role=WorkspaceRole.VIEWER,
    )
    return Account(principal=principal, user_id=None, display_name="", workspace=demo, workspaces=workspaces)


async def create_workspace_with_profile(
    repo: AccountRepository, session: AsyncSession, *, owner: uuid.UUID, name: str, personal: bool
) -> Workspace:
    """A workspace, its owner membership and a primary profile (system scope)."""
    workspace = await repo.create_workspace(name=name, slug=new_slug(name), personal=personal, plan="free")
    await repo.add_membership(workspace_id=workspace.id, user_id=owner, role="owner")
    session.add(
        Profile(
            workspace_id=workspace.id,
            display_name=name,
            target_roles=[],
            skills=[],
            years_experience=Decimal(0),
            policy=EligibilityPolicy().model_dump(mode="json"),
        )
    )
    await session.flush()
    return workspace


async def _provision(repo: AccountRepository, session: AsyncSession, principal: Principal, settings: Settings) -> User:
    if principal.provider is None or principal.provider_subject is None:
        raise AuthenticationError("authentication required")
    if settings.signup_policy == "closed" and not principal.platform_admin:
        raise PermissionDeniedError("sign-ups are closed")
    user = await repo.create_user(
        provider=principal.provider, subject=principal.provider_subject, display_name=principal.login or ""
    )
    # Open sign-up gives everyone a personal workspace; "invite" users join via invitations.
    if settings.signup_policy == "open" and not principal.platform_admin:
        label = f"{principal.login}'s workspace" if principal.login else "My workspace"
        await create_workspace_with_profile(repo, session, owner=user.id, name=label, personal=True)
    logger.info("account.provisioned", provider=principal.provider, user_id=str(user.id))
    return user


async def resolve_account(session: AsyncSession, principal: Principal, settings: Settings) -> Account:
    """Must run in system scope. Never trusts role or workspace claims from the token."""
    if not principal.authenticated:
        return _viewer_of_demo(principal, settings, [])
    repo = AccountRepository(session)
    if principal.provider is None or principal.provider_subject is None:
        raise AuthenticationError("authentication required")
    user = await repo.user_for_identity(principal.provider, principal.provider_subject)
    if user is None:
        user = await _provision(repo, session, principal, settings)
    if principal.platform_admin:
        # Platform admins (OWNER_GITHUB_IDS) always own the Default workspace.
        await repo.add_membership(workspace_id=DEFAULT_WORKSPACE_ID, user_id=user.id, role="owner")
    workspaces = [_summary(m) for m in await repo.memberships(user.id)]
    if not workspaces:
        # Signed in but not a member anywhere yet (invite-only sign-up): they can accept an
        # invitation, and browse the public demo read-only if it is enabled.
        demo = _viewer_of_demo(principal, settings, []).workspace if settings.public_demo_enabled else None
        return Account(principal=principal, user_id=user.id, display_name=user.display_name, workspace=demo)
    selected = next((w for w in workspaces if w.id == principal.workspace_hint), workspaces[0])
    return Account(
        principal=principal,
        user_id=user.id,
        display_name=user.display_name,
        workspace=selected,
        workspaces=workspaces,
    )


@dataclass(frozen=True, slots=True)
class IssuedInvitation:
    id: uuid.UUID
    token: str  # shown once; only its hash is stored
    expires_at: datetime


async def issue_invitation(
    repo: AccountRepository, account: Account, *, email: str | None, role: WorkspaceRole
) -> IssuedInvitation:
    """Workspace scope. Admins invite members/admins; only owners can invite owners."""
    inviter = account.require(WorkspaceRole.ADMIN)
    if role > inviter.role:
        raise PermissionDeniedError("you cannot invite someone with a higher role than yours")
    now = datetime.now(tz=UTC)
    seats = await repo.member_count() + len(await repo.pending_invitations(now))
    require_capacity(
        inviter.plan, what="members (including pending invitations)", used=seats, limit=limits_for(inviter.plan).members
    )
    token = secrets.token_urlsafe(INVITATION_TOKEN_BYTES)
    expires_at = now + INVITATION_TTL
    invitation = await repo.create_invitation(
        email=email, role=role.label, token_hash=hash_token(token), invited_by=account.user_id, expires_at=expires_at
    )
    return IssuedInvitation(id=invitation.id, token=token, expires_at=expires_at)


async def accept_invitation(repo: AccountRepository, account: Account, token: str) -> uuid.UUID:
    """System scope. Single-use, unexpired, unrevoked; returns the joined workspace id."""
    if account.user_id is None:
        raise AuthenticationError("sign in to accept an invitation")
    invitation = await repo.invitation_by_token_hash(hash_token(token))
    now = datetime.now(tz=UTC)
    if invitation is None or invitation.revoked_at is not None or invitation.expires_at <= now:
        raise NotFoundError("invitation not found or expired")
    if invitation.accepted_at is not None:
        if invitation.accepted_by == account.user_id:
            return invitation.workspace_id  # idempotent re-click
        raise ConflictError("invitation already used")
    workspace = await repo.workspace(invitation.workspace_id)
    if workspace is None:
        raise NotFoundError("invitation not found or expired")
    plan = effective_plan(
        workspace.plan, subscription_status=workspace.subscription_status, grace_until=workspace.grace_until, now=now
    )
    if not await repo.is_member(invitation.workspace_id, account.user_id):
        members = await repo.member_count_in(invitation.workspace_id)
        require_capacity(plan, what="members", used=members, limit=limits_for(plan).members)
    await repo.add_membership(workspace_id=invitation.workspace_id, user_id=account.user_id, role=invitation.role)
    await repo.mark_accepted(invitation, user_id=account.user_id, now=now)
    logger.info("invitation.accepted", workspace_id=str(invitation.workspace_id), user_id=str(account.user_id))
    return invitation.workspace_id
