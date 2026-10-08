"""Email magic-link sign-in, workspace export and deletion."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select, update

from jobpulse.api.deps import get_ctx, get_temporal
from jobpulse.core.config import Settings
from jobpulse.db.models import DEFAULT_WORKSPACE_ID, EmailLoginToken, Source, Workspace
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE
from jobpulse.main import create_app
from jobpulse.services.context import AppContext
from jobpulse.services.email_login import RESEND_URL, email_subject
from tests.auth_helpers import OWNER_ID, make_token
from tests.integration.test_api import FakeTemporal

pytestmark = pytest.mark.integration

ALICE, BOB = 9301, 9302
MAIL = {"resend_api_key": SecretStr("re_test"), "notification_from_email": "JobPulse <no-reply@jobpulse.example>"}


def user(github_id: int, workspace: str | None = None) -> dict[str, str]:
    claims = {"wid": workspace} if workspace else {}
    return {"Authorization": f"Bearer {make_token(github_id, login=f'u{github_id}', **claims)}"}


@asynccontextmanager
async def api_for(settings: Settings, ctx: AppContext) -> AsyncGenerator[httpx.AsyncClient]:
    context = replace(ctx, settings=settings)
    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: context
    temporal = FakeTemporal()
    app.dependency_overrides[get_temporal] = lambda: temporal
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.fixture
async def api(settings: Settings, ctx: AppContext) -> AsyncGenerator[httpx.AsyncClient]:
    async with api_for(settings.model_copy(update=MAIL), ctx) as client:
        yield client


def link_token(mock: respx.Route) -> str:
    text = mock.calls.last.request.content.decode()
    return text.split("token=", 1)[1].split("\\n", 1)[0].split('"', 1)[0]


@respx.mock
async def test_magic_link_signs_in_once(api: httpx.AsyncClient) -> None:
    sent = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "email_1"}))
    started = await api.post("/api/v1/auth/email/start", json={"email": "Ada@Example.com"})
    assert started.status_code == 202
    token = link_token(sent)
    verified = await api.post("/api/v1/auth/email/verify", json={"token": token})
    assert verified.json() == {"subject": email_subject("ada@example.com"), "email": "ada@example.com"}
    replay = await api.post("/api/v1/auth/email/verify", json={"token": token})
    assert replay.status_code == 404  # single use

    # The verified subject is a first-class identity: it provisions an account.
    session = {"Authorization": f"Bearer {make_token(sub='email:' + email_subject('ada@example.com'), login='ada')}"}
    me = (await api.get("/api/v1/me", headers=session)).json()
    assert me["authenticated"] is True
    assert me["workspace"]["personal"] is True


@respx.mock
async def test_magic_links_expire_and_are_throttled(api: httpx.AsyncClient, ctx: AppContext) -> None:
    sent = respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "email_1"}))
    await api.post("/api/v1/auth/email/start", json={"email": "late@example.com"})
    token = link_token(sent)
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        await session.execute(update(EmailLoginToken).values(expires_at=datetime.now(tz=UTC) - timedelta(seconds=1)))
    assert (await api.post("/api/v1/auth/email/verify", json={"token": token})).status_code == 404


async def test_magic_link_throttle_per_address(redis_settings: Settings, ctx: AppContext) -> None:
    with respx.mock:
        respx.post(RESEND_URL).mock(return_value=httpx.Response(200, json={"id": "email_1"}))
        async with api_for(redis_settings.model_copy(update=MAIL), ctx) as api:
            statuses = [
                (await api.post("/api/v1/auth/email/start", json={"email": "spam@example.com"})).status_code
                for _ in range(6)
            ]
    assert statuses == [202] * 5 + [429]


async def test_email_sign_in_requires_delivery_outside_development(settings: Settings, ctx: AppContext) -> None:
    async with api_for(settings.model_copy(update={"environment": "staging"}), ctx) as api:
        response = await api.post("/api/v1/auth/email/start", json={"email": "x@example.com"})
    assert response.status_code == 503


async def test_unknown_tokens_are_rejected(api: httpx.AsyncClient) -> None:
    assert (await api.post("/api/v1/auth/email/verify", json={"token": "x" * 43})).status_code == 404
    assert (await api.post("/api/v1/auth/email/verify", json={"token": "bad token"})).status_code == 422


async def test_export_contains_only_this_workspace(api: httpx.AsyncClient) -> None:
    await api.patch("/api/v1/profile", json={"display_name": "Alice profile"}, headers=user(ALICE))
    await api.patch("/api/v1/profile", json={"display_name": "Bob profile"}, headers=user(BOB))
    exported = await api.get("/api/v1/workspace/export", headers=user(ALICE))
    assert exported.status_code == 200
    assert exported.headers["content-disposition"].startswith("attachment;")
    document = exported.json()
    assert document["format"] == "jobpulse.workspace-export"
    assert [p["display_name"] for p in document["profiles"]] == ["Alice profile"]
    assert "Bob profile" not in exported.text


async def test_delete_workspace(api: httpx.AsyncClient, ctx: AppContext) -> None:
    alice = user(ALICE)
    team = (await api.post("/api/v1/workspaces", json={"name": "Doomed"}, headers=alice)).json()
    alice_team = user(ALICE, team["id"])
    board = {
        "kind": "greenhouse",
        "name": "Solo",
        "company_name": "Solo",
        "company_domain": "solo.io",
        "board_token": "solo",
    }
    source_id = (await api.post("/api/v1/sources", json=board, headers=alice_team)).json()["id"]

    wrong = await api.request("DELETE", "/api/v1/workspace", json={"confirm_name": "doomed"}, headers=alice_team)
    assert wrong.status_code == 409
    deleted = await api.request("DELETE", "/api/v1/workspace", json={"confirm_name": "Doomed"}, headers=alice_team)
    assert deleted.status_code == 204

    me = (await api.get("/api/v1/me", headers=alice_team)).json()
    assert team["id"] not in {w["id"] for w in me["workspaces"]}
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        assert await session.get(Workspace, uuid.UUID(team["id"])) is None
        source = (await session.execute(select(Source).where(Source.id == uuid.UUID(source_id)))).scalar_one()
    assert source.enabled is False  # nobody follows it any more: polling stops, catalogue row stays


async def test_demo_workspace_cannot_be_deleted(api: httpx.AsyncClient) -> None:
    admin = {"Authorization": f"Bearer {make_token(OWNER_ID)}"}
    me = (await api.get("/api/v1/me", headers=admin)).json()
    assert me["workspace"]["id"] == str(DEFAULT_WORKSPACE_ID)
    response = await api.request(
        "DELETE", "/api/v1/workspace", json={"confirm_name": me["workspace"]["name"]}, headers=admin
    )
    assert response.status_code == 409


async def test_only_owners_export_or_delete(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/workspace/export")).status_code == 403  # anonymous viewer
    response = await api.request("DELETE", "/api/v1/workspace", json={"confirm_name": "x"})
    assert response.status_code == 403
