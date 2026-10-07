"""/api/v1/me, workspaces, members and invitations."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Response, status

from jobpulse.api.deps import Ctx
from jobpulse.api.schemas import (
    InvitationAccept,
    InvitationAccepted,
    InvitationCreate,
    InvitationIssued,
    InvitationOut,
    Me,
    MemberOut,
    MemberPatch,
    WorkspaceCreate,
    WorkspaceOut,
    WorkspacePatch,
)
from jobpulse.api.tenancy import Admin, CurrentAccount, Member, Session, WorkspaceOwner, system_section
from jobpulse.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from jobpulse.db.models import Workspace
from jobpulse.repositories.accounts import AccountRepository
from jobpulse.repositories.activity import AuditRepository
from jobpulse.services.accounts import (
    Account,
    WorkspaceRole,
    WorkspaceSummary,
    accept_invitation,
    create_workspace_with_profile,
    issue_invitation,
)

router = APIRouter(prefix="/api/v1", tags=["workspaces"])


def _workspace_out(summary: WorkspaceSummary) -> WorkspaceOut:
    return WorkspaceOut(
        id=summary.id,
        name=summary.name,
        slug=summary.slug,
        plan=summary.plan,
        personal=summary.personal,
        role=summary.role.label,  # type: ignore[arg-type]
    )


@router.get("/me", response_model=Me)
async def me(account: CurrentAccount) -> Me:
    principal = account.principal
    return Me(
        subject=principal.subject,
        login=principal.login,
        authenticated=principal.authenticated,
        user_id=account.user_id,
        display_name=account.display_name,
        platform_admin=principal.platform_admin,
        role=account.role.label if account.role is not None else None,  # type: ignore[arg-type]
        workspace=_workspace_out(account.workspace) if account.workspace else None,
        workspaces=[_workspace_out(w) for w in account.workspaces],
    )


@router.post("/workspaces", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(body: WorkspaceCreate, account: CurrentAccount, session: Session) -> WorkspaceOut:
    """Any signed-in user can create a team workspace and becomes its owner."""
    if account.user_id is None:
        raise PermissionDeniedError("sign in to create a workspace")
    async with system_section(session, account):
        repo = AccountRepository(session)
        workspace = await create_workspace_with_profile(
            repo, session, owner=account.user_id, name=body.name, personal=False
        )
        await AuditRepository(session).record(
            actor=account.principal.actor,
            action="workspace.create",
            entity_type="workspace",
            entity_id=str(workspace.id),
            workspace_id=workspace.id,
        )
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        plan=workspace.plan,
        personal=False,
        role="owner",
    )


@router.patch("/workspace", response_model=WorkspaceOut)
async def rename_workspace(body: WorkspacePatch, account: WorkspaceOwner, session: Session) -> WorkspaceOut:
    current = account.require(WorkspaceRole.OWNER)
    workspace = await session.get(Workspace, current.id, with_for_update=True)
    if workspace is None:
        raise NotFoundError("workspace not found")
    workspace.name = body.name
    await AuditRepository(session).record(
        actor=account.principal.actor, action="workspace.rename", entity_type="workspace", entity_id=str(current.id)
    )
    return WorkspaceOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        plan=workspace.plan,
        personal=workspace.personal,
        role="owner",
    )


# ------------------------------------------------------------------ members


@router.get("/workspace/members", response_model=list[MemberOut])
async def list_members(account: Member, session: Session) -> list[MemberOut]:
    rows = await AccountRepository(session).members()
    return [
        MemberOut(
            user_id=row.user.id,
            display_name=row.user.display_name,
            role=row.role,  # type: ignore[arg-type]
            joined_at=row.joined_at,
            you=row.user.id == account.user_id,
        )
        for row in rows
    ]


def _check_owner_rules(account: Account, *, target_role: str, new_role: str | None) -> None:
    """Owner grants/removals need an owner; admins manage members and admins."""
    actor = account.require(WorkspaceRole.ADMIN)
    touches_owner = target_role == "owner" or new_role == "owner"
    if touches_owner and actor.role < WorkspaceRole.OWNER:
        raise PermissionDeniedError("only owners can grant or remove the owner role")


@router.patch("/workspace/members/{user_id}", response_model=MemberOut)
async def change_member_role(user_id: uuid.UUID, body: MemberPatch, account: Admin, session: Session) -> MemberOut:
    repo = AccountRepository(session)
    membership = await repo.membership(user_id)
    if membership is None:
        raise NotFoundError("member not found")
    _check_owner_rules(account, target_role=membership.role, new_role=body.role)
    if membership.role == "owner" and body.role != "owner" and await repo.owner_count() <= 1:
        raise ConflictError("a workspace needs at least one owner")
    previous, membership.role = membership.role, body.role
    await session.flush()
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="member.role",
        entity_type="user",
        entity_id=str(user_id),
        payload={"from": previous, "to": body.role},
    )
    member = next(row for row in await repo.members() if row.user.id == user_id)
    return MemberOut(
        user_id=user_id,
        display_name=member.user.display_name,
        role=body.role,
        joined_at=member.joined_at,
        you=user_id == account.user_id,
    )


@router.delete("/workspace/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(user_id: uuid.UUID, account: Member, session: Session) -> Response:
    """Admins remove members; anyone can leave. The last owner can do neither."""
    repo = AccountRepository(session)
    membership = await repo.membership(user_id)
    if membership is None:
        raise NotFoundError("member not found")
    if user_id != account.user_id:
        _check_owner_rules(account, target_role=membership.role, new_role=None)
    if membership.role == "owner" and await repo.owner_count() <= 1:
        raise ConflictError("a workspace needs at least one owner")
    await repo.remove_membership(membership)
    await AuditRepository(session).record(
        actor=account.principal.actor, action="member.remove", entity_type="user", entity_id=str(user_id)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ invitations


@router.get("/workspace/invitations", response_model=list[InvitationOut])
async def list_invitations(_: Admin, session: Session) -> list[InvitationOut]:
    pending = await AccountRepository(session).pending_invitations(datetime.now(tz=UTC))
    return [InvitationOut.model_validate(item) for item in pending]


@router.post("/workspace/invitations", response_model=InvitationIssued, status_code=status.HTTP_201_CREATED)
async def invite(body: InvitationCreate, account: Admin, session: Session, ctx: Ctx) -> InvitationIssued:
    issued = await issue_invitation(
        AccountRepository(session), account, email=body.email, role=WorkspaceRole.parse(body.role)
    )
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="invitation.create",
        entity_type="invitation",
        entity_id=str(issued.id),
        payload={"role": body.role, "email": body.email},
    )
    base = ctx.settings.dashboard_base_url.rstrip("/")
    return InvitationIssued(
        id=issued.id, role=body.role, expires_at=issued.expires_at, invite_url=f"{base}/invite/{issued.token}"
    )


@router.delete("/workspace/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(invitation_id: uuid.UUID, account: Admin, session: Session) -> Response:
    invitation = await AccountRepository(session).invitation(invitation_id)
    if invitation is None or invitation.accepted_at is not None:
        raise NotFoundError("pending invitation not found")
    invitation.revoked_at = datetime.now(tz=UTC)
    await AuditRepository(session).record(
        actor=account.principal.actor,
        action="invitation.revoke",
        entity_type="invitation",
        entity_id=str(invitation_id),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/invitations/accept", response_model=InvitationAccepted)
async def accept(body: InvitationAccept, account: CurrentAccount, session: Session) -> InvitationAccepted:
    async with system_section(session, account):
        workspace_id = await accept_invitation(AccountRepository(session), account, body.token)
        await AuditRepository(session).record(
            actor=account.principal.actor,
            action="invitation.accept",
            entity_type="workspace",
            entity_id=str(workspace_id),
            workspace_id=workspace_id,
        )
    return InvitationAccepted(workspace_id=workspace_id)
