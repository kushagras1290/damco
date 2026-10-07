"""Shared, deduplicated job boards: one poller per board, per-workspace follow/pause."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest
from sqlalchemy import select

from jobpulse.api.deps import get_ctx, get_temporal
from jobpulse.api.routes.events import _audience
from jobpulse.core.config import Settings
from jobpulse.core.security import decode_token
from jobpulse.db.models import Source
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE
from jobpulse.main import create_app
from jobpulse.services.context import AppContext
from jobpulse.services.ingestion import IngestionService
from tests.auth_helpers import OWNER_ID, make_token
from tests.integration.test_api import FakeTemporal

pytestmark = pytest.mark.integration

ALICE, BOB = 8101, 8102
BOARD = {"kind": "greenhouse", "company_name": "Acme", "company_domain": "acme.io"}


def user(github_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(github_id, login=f'u{github_id}')}"}


@pytest.fixture
def temporal() -> FakeTemporal:
    return FakeTemporal()


@pytest.fixture
async def api(settings: Settings, ctx: AppContext, temporal: FakeTemporal) -> AsyncGenerator[httpx.AsyncClient]:
    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: ctx
    app.dependency_overrides[get_temporal] = lambda: temporal
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


async def sources_in_catalogue(ctx: AppContext) -> list[Source]:
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        return list((await session.execute(select(Source))).scalars().all())


async def test_same_board_is_shared_not_duplicated(
    api: httpx.AsyncClient, ctx: AppContext, temporal: FakeTemporal
) -> None:
    alice = await api.post(
        "/api/v1/sources", json={**BOARD, "name": "Acme jobs", "board_token": "acme"}, headers=user(ALICE)
    )
    bob = await api.post("/api/v1/sources", json={**BOARD, "name": "ACME", "board_token": "ACME"}, headers=user(BOB))
    assert alice.status_code == bob.status_code == 201
    assert alice.json()["id"] == bob.json()["id"]
    assert alice.json()["editable"] is True  # sole follower at the time
    assert bob.json()["editable"] is False  # now shared
    assert len(await sources_in_catalogue(ctx)) == 1
    assert len(temporal.started) == 1  # one poller for both workspaces
    again = await api.post("/api/v1/sources", json={**BOARD, "name": "x", "board_token": "acme"}, headers=user(BOB))
    assert again.status_code == 409


async def test_pause_is_per_workspace_and_polling_stops_with_the_last_follower(
    api: httpx.AsyncClient, ctx: AppContext
) -> None:
    body = {**BOARD, "name": "Acme", "board_token": "acme"}
    source_id = (await api.post("/api/v1/sources", json=body, headers=user(ALICE))).json()["id"]
    await api.post("/api/v1/sources", json=body, headers=user(BOB))

    paused = await api.patch(f"/api/v1/sources/{source_id}", json={"enabled": False}, headers=user(ALICE))
    assert paused.json()["paused"] is True
    assert paused.json()["enabled"] is True  # Bob still follows: the board keeps polling
    bob_view = (await api.get(f"/api/v1/sources/{source_id}", headers=user(BOB))).json()
    assert bob_view["paused"] is False

    targets = await IngestionService(ctx).evaluation_targets(source_id)
    alice_ws = (await api.get("/api/v1/me", headers=user(ALICE))).json()["workspace"]["id"]
    assert alice_ws not in {t.workspace_id for t in targets.targets}  # no evaluations while paused

    both = await api.patch(f"/api/v1/sources/{source_id}", json={"enabled": False}, headers=user(BOB))
    assert both.json()["enabled"] is False  # nobody active: polling stops
    assert (await api.post(f"/api/v1/sources/{source_id}/sync", headers=user(BOB))).status_code == 409


async def test_unfollow_keeps_the_board_for_others(api: httpx.AsyncClient, ctx: AppContext) -> None:
    body = {**BOARD, "name": "Acme", "board_token": "acme"}
    source_id = (await api.post("/api/v1/sources", json=body, headers=user(ALICE))).json()["id"]
    await api.post("/api/v1/sources", json=body, headers=user(BOB))
    assert (await api.delete(f"/api/v1/sources/{source_id}", headers=user(ALICE))).status_code == 204
    assert (await api.get("/api/v1/sources", headers=user(ALICE))).json()["total"] == 0
    assert (await api.get(f"/api/v1/sources/{source_id}", headers=user(ALICE))).status_code == 404
    assert (await api.get(f"/api/v1/sources/{source_id}", headers=user(BOB))).json()["enabled"] is True
    assert (await api.delete(f"/api/v1/sources/{source_id}", headers=user(BOB))).status_code == 204
    (source,) = await sources_in_catalogue(ctx)
    assert source.enabled is False  # last follower left: catalogue row kept, polling off


async def test_shared_settings_need_sole_follower_or_platform_admin(api: httpx.AsyncClient) -> None:
    body = {**BOARD, "name": "Acme", "board_token": "acme"}
    source_id = (await api.post("/api/v1/sources", json=body, headers=user(ALICE))).json()["id"]
    renamed = await api.patch(f"/api/v1/sources/{source_id}", json={"name": "Acme (mine)"}, headers=user(ALICE))
    assert renamed.json()["name"] == "Acme (mine)"  # sole follower
    await api.post("/api/v1/sources", json=body, headers=user(BOB))
    blocked = await api.patch(f"/api/v1/sources/{source_id}", json={"poll_interval_seconds": 120}, headers=user(BOB))
    assert blocked.status_code == 409
    admin = {"Authorization": f"Bearer {make_token(OWNER_ID)}"}
    await api.post("/api/v1/sources", json=body, headers=admin)  # Default workspace follows too
    allowed = await api.patch(f"/api/v1/sources/{source_id}", json={"poll_interval_seconds": 600}, headers=admin)
    assert allowed.json()["poll_interval_seconds"] == 600


async def test_unfollowed_sources_are_invisible(api: httpx.AsyncClient) -> None:
    body = {**BOARD, "name": "Acme", "board_token": "acme"}
    source_id = (await api.post("/api/v1/sources", json=body, headers=user(ALICE))).json()["id"]
    for method, path in (
        ("GET", f"/api/v1/sources/{source_id}"),
        ("PATCH", f"/api/v1/sources/{source_id}"),
        ("POST", f"/api/v1/sources/{source_id}/sync"),
        ("DELETE", f"/api/v1/sources/{source_id}"),
    ):
        response = await api.request(
            method, path, json={"enabled": True} if method == "PATCH" else None, headers=user(BOB)
        )
        assert response.status_code == 404, (method, path)
    assert (await api.get(f"/api/v1/sources/{uuid.uuid4()}", headers=user(ALICE))).status_code == 404


async def test_event_stream_audience_is_the_callers_workspace(
    api: httpx.AsyncClient, settings: Settings, ctx: AppContext
) -> None:
    body = {**BOARD, "name": "Acme", "board_token": "acme"}
    source_id = (await api.post("/api/v1/sources", json=body, headers=user(ALICE))).json()["id"]
    await api.get("/api/v1/me", headers=user(BOB))  # Bob exists but follows nothing
    alice = await _audience(ctx, decode_token(make_token(ALICE, login=f"u{ALICE}"), settings))
    bob = await _audience(ctx, decode_token(make_token(BOB, login=f"u{BOB}"), settings))
    alice_ws = (await api.get("/api/v1/me", headers=user(ALICE))).json()["workspace"]["id"]
    assert alice.workspace_id == alice_ws
    assert alice.source_ids == frozenset({source_id})
    assert bob.workspace_id not in {None, alice_ws}
    assert bob.source_ids == frozenset()
