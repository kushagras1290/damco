"""Accounts, workspaces, roles and invitations through the HTTP API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, update

from jobpulse.api.deps import get_ctx
from jobpulse.core.config import Settings
from jobpulse.db.models import DEFAULT_WORKSPACE_ID, Identity, Invitation
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE
from jobpulse.main import create_app
from jobpulse.services.context import AppContext
from tests.auth_helpers import OWNER_ID, make_token

pytestmark = pytest.mark.integration

ALICE, BOB, CAROL = 7001, 7002, 7003


def as_user(github_id: int, login: str, workspace: str | None = None) -> dict[str, str]:
    claims = {"wid": workspace} if workspace else {}
    return {"Authorization": f"Bearer {make_token(github_id, login=login, **claims)}"}


@asynccontextmanager
async def api_for(settings: Settings, ctx: AppContext) -> AsyncGenerator[httpx.AsyncClient]:
    context = replace(ctx, settings=settings)
    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: context
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def api(settings: Settings, ctx: AppContext) -> AsyncGenerator[httpx.AsyncClient]:
    async with api_for(settings, ctx) as client:
        yield client


def token_of(invite_url: str) -> str:
    return invite_url.rsplit("/", 1)[1]


async def invite_token(api: httpx.AsyncClient, headers: dict[str, str], role: str = "member") -> str:
    issued = await api.post("/api/v1/workspace/invitations", json={"role": role}, headers=headers)
    assert issued.status_code == 201, issued.text
    return token_of(issued.json()["invite_url"])


async def accept(api: httpx.AsyncClient, token: str, headers: dict[str, str]) -> httpx.Response:
    return await api.post("/api/v1/invitations/accept", json={"token": token}, headers=headers)


async def test_first_sign_in_creates_a_personal_workspace(api: httpx.AsyncClient) -> None:
    first = (await api.get("/api/v1/me", headers=as_user(ALICE, "alice"))).json()
    again = (await api.get("/api/v1/me", headers=as_user(ALICE, "alice"))).json()
    assert first["role"] == "owner"
    assert first["workspace"]["personal"] is True
    assert first["workspace"]["name"] == "alice's workspace"
    assert again["user_id"] == first["user_id"]  # provisioned exactly once
    assert again["workspace"]["id"] == first["workspace"]["id"]
    profile = await api.get("/api/v1/profile", headers=as_user(ALICE, "alice"))
    assert profile.status_code == 200


async def test_concurrent_first_requests_create_one_account(api: httpx.AsyncClient, ctx: AppContext) -> None:
    responses = await asyncio.gather(*(api.get("/api/v1/me", headers=as_user(BOB, "bob")) for _ in range(5)))
    assert [r.status_code for r in responses] == [200] * 5  # racing sign-ups all succeed
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        count = (
            await session.execute(select(func.count()).select_from(Identity).where(Identity.subject == str(BOB)))
        ).scalar_one()
    assert count == 1
    assert (await api.get("/api/v1/me", headers=as_user(BOB, "bob"))).status_code == 200


async def test_invite_accept_and_roles(api: httpx.AsyncClient) -> None:
    alice = as_user(ALICE, "alice")
    workspace = (await api.post("/api/v1/workspaces", json={"name": "Acme Talent"}, headers=alice)).json()
    alice_team = as_user(ALICE, "alice", workspace["id"])
    assert workspace["role"] == "owner"

    issued = await api.post("/api/v1/workspace/invitations", json={"role": "member"}, headers=alice_team)
    assert issued.status_code == 201
    token = token_of(issued.json()["invite_url"])
    pending = (await api.get("/api/v1/workspace/invitations", headers=alice_team)).json()
    assert [p["id"] for p in pending] == [issued.json()["id"]]
    assert "token" not in pending[0]  # the secret is shown once, never listed

    accepted = await api.post("/api/v1/invitations/accept", json={"token": token}, headers=as_user(BOB, "bob"))
    assert accepted.json() == {"workspace_id": workspace["id"]}
    again = await api.post("/api/v1/invitations/accept", json={"token": token}, headers=as_user(BOB, "bob"))
    assert again.status_code == 200  # idempotent for the same user
    reused = await api.post("/api/v1/invitations/accept", json={"token": token}, headers=as_user(CAROL, "carol"))
    assert reused.status_code == 409  # single use

    bob_team = as_user(BOB, "bob", workspace["id"])
    bob_me = (await api.get("/api/v1/me", headers=bob_team)).json()
    assert bob_me["workspace"]["id"] == workspace["id"]
    assert bob_me["role"] == "member"
    assert len(bob_me["workspaces"]) == 2  # personal + team

    # Members cannot manage the workspace.
    source = {"kind": "lever", "name": "Acme", "company_name": "Acme", "company_domain": "acme.io", "board_token": "a"}
    assert (await api.post("/api/v1/sources", json=source, headers=bob_team)).status_code == 403
    assert (await api.post("/api/v1/workspace/invitations", json={}, headers=bob_team)).status_code == 403
    assert (await api.patch("/api/v1/profile", json={"display_name": "Bob"}, headers=bob_team)).status_code == 200

    members = (await api.get("/api/v1/workspace/members", headers=bob_team)).json()
    assert sorted(m["role"] for m in members) == ["member", "owner"]
    bob_id = next(m["user_id"] for m in members if m["you"])

    promoted = await api.patch(f"/api/v1/workspace/members/{bob_id}", json={"role": "admin"}, headers=alice_team)
    assert promoted.json()["role"] == "admin"
    # Admins cannot hand out ownership or invite owners.
    assert (
        await api.patch(f"/api/v1/workspace/members/{bob_id}", json={"role": "owner"}, headers=bob_team)
    ).status_code == 403
    invite_owner = await api.post("/api/v1/workspace/invitations", json={"role": "owner"}, headers=bob_team)
    assert invite_owner.status_code == 403
    assert (await api.post("/api/v1/sources", json=source, headers=bob_team)).status_code == 201  # admin can


async def test_last_owner_is_protected(api: httpx.AsyncClient) -> None:
    alice = as_user(ALICE, "alice")
    me = (await api.get("/api/v1/me", headers=alice)).json()
    leave = await api.delete(f"/api/v1/workspace/members/{me['user_id']}", headers=alice)
    assert leave.status_code == 409
    demote = await api.patch(f"/api/v1/workspace/members/{me['user_id']}", json={"role": "member"}, headers=alice)
    assert demote.status_code == 409


async def test_members_can_leave_and_lose_access(api: httpx.AsyncClient) -> None:
    alice = as_user(ALICE, "alice")
    workspace = (await api.post("/api/v1/workspaces", json={"name": "Team"}, headers=alice)).json()
    alice_team = as_user(ALICE, "alice", workspace["id"])
    token = token_of(
        (await api.post("/api/v1/workspace/invitations", json={}, headers=alice_team)).json()["invite_url"]
    )
    await api.post("/api/v1/invitations/accept", json={"token": token}, headers=as_user(BOB, "bob"))
    bob_team = as_user(BOB, "bob", workspace["id"])
    bob_id = (await api.get("/api/v1/me", headers=bob_team)).json()["user_id"]
    assert (await api.delete(f"/api/v1/workspace/members/{bob_id}", headers=bob_team)).status_code == 204
    after = (await api.get("/api/v1/me", headers=bob_team)).json()
    assert after["workspace"]["id"] != workspace["id"]  # the stale selection no longer applies


async def test_expired_and_revoked_invitations_fail(api: httpx.AsyncClient, ctx: AppContext) -> None:
    alice = as_user(ALICE, "alice")
    first = (await api.post("/api/v1/workspace/invitations", json={}, headers=alice)).json()
    second = (await api.post("/api/v1/workspace/invitations", json={}, headers=alice)).json()
    assert (await api.delete(f"/api/v1/workspace/invitations/{second['id']}", headers=alice)).status_code == 204
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        await session.execute(
            update(Invitation)
            .where(Invitation.id == first["id"])
            .values(expires_at=datetime.now(tz=UTC) - timedelta(seconds=1))
        )
    bob = as_user(BOB, "bob")
    for issued in (first, second):
        response = await api.post(
            "/api/v1/invitations/accept", json={"token": token_of(issued["invite_url"])}, headers=bob
        )
        assert response.status_code == 404


async def test_only_owners_rename(api: httpx.AsyncClient) -> None:
    alice = as_user(ALICE, "alice")
    renamed = await api.patch("/api/v1/workspace", json={"name": "Alice Search"}, headers=alice)
    assert renamed.json()["name"] == "Alice Search"
    anonymous = await api.patch("/api/v1/workspace", json={"name": "Hacked"})
    assert anonymous.status_code == 403


async def test_invite_only_signup(settings: Settings, ctx: AppContext) -> None:
    async with api_for(settings, ctx) as open_api:
        token = token_of(
            (
                await open_api.post("/api/v1/workspace/invitations", json={}, headers=as_user(OWNER_ID, "octocat"))
            ).json()["invite_url"]
        )
    invite_only = settings.model_copy(update={"signup_policy": "invite"})
    async with api_for(invite_only, ctx) as api:
        stranger = (await api.get("/api/v1/me", headers=as_user(CAROL, "carol"))).json()
        assert stranger["workspaces"] == []  # no personal workspace under invite-only
        assert stranger["role"] == "viewer"  # may browse the public demo read-only
        await api.post("/api/v1/invitations/accept", json={"token": token}, headers=as_user(CAROL, "carol"))
        joined = (await api.get("/api/v1/me", headers=as_user(CAROL, "carol"))).json()
    assert joined["workspace"]["id"] == str(DEFAULT_WORKSPACE_ID)
    assert joined["role"] == "member"


async def test_closed_signup_admits_only_platform_admins(settings: Settings, ctx: AppContext) -> None:
    closed = settings.model_copy(update={"signup_policy": "closed"})
    async with api_for(closed, ctx) as api:
        stranger = await api.get("/api/v1/me", headers=as_user(CAROL, "carol"))
        admin = await api.get("/api/v1/me", headers=as_user(OWNER_ID, "octocat"))
    assert stranger.status_code == 403
    assert admin.json()["workspace"]["id"] == str(DEFAULT_WORKSPACE_ID)
