"""Plan limits and Razorpay billing through the HTTP API (provider mocked with respx)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import update

from jobpulse.api.deps import get_ctx, get_temporal
from jobpulse.core.config import Settings
from jobpulse.db.models import Workspace
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE
from jobpulse.main import create_app
from jobpulse.services.context import AppContext
from tests.auth_helpers import OWNER_ID, make_token
from tests.integration.test_api import FakeTemporal

pytestmark = pytest.mark.integration

ALICE, BOB = 9101, 9102
WEBHOOK_SECRET = "whsec_integration"
RAZORPAY = "https://api.razorpay.test/v1"
BILLING = {
    "razorpay_key_id": "rzp_test_key",
    "razorpay_key_secret": SecretStr("rzp_test_secret"),
    "razorpay_webhook_secret": SecretStr(WEBHOOK_SECRET),
    "razorpay_plan_pro": "plan_pro",
    "razorpay_plan_team": "plan_team",
    "razorpay_api_base": RAZORPAY,
}


def user(github_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(github_id, login=f'u{github_id}')}"}


def board(token: str) -> dict[str, str]:
    return {
        "kind": "greenhouse",
        "name": token,
        "company_name": token,
        "company_domain": f"{token}.io",
        "board_token": token,
    }


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
    async with api_for(settings.model_copy(update=BILLING), ctx) as client:
        yield client


def webhook(
    event_type: str,
    *,
    subscription_id: str,
    status: str,
    plan_id: str,
    workspace_id: str,
    event_id: str | None = None,
    current_end: datetime | None = None,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(
        {
            "entity": "event",
            "event": event_type,
            "payload": {
                "subscription": {
                    "entity": {
                        "id": subscription_id,
                        "plan_id": plan_id,
                        "status": status,
                        "current_end": int((current_end or datetime.now(tz=UTC) + timedelta(days=30)).timestamp()),
                        "notes": {"workspace_id": workspace_id, "plan": "team"},
                    }
                }
            },
        }
    ).encode()
    signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "x-razorpay-signature": signature,
        "x-razorpay-event-id": event_id or f"evt_{uuid.uuid4().hex}",
        "content-type": "application/json",
    }
    return body, headers


async def workspace_of(api: httpx.AsyncClient, github_id: int) -> str:
    return str((await api.get("/api/v1/me", headers=user(github_id))).json()["workspace"]["id"])


async def test_free_plan_limits_followed_boards(api: httpx.AsyncClient) -> None:
    for index in range(5):
        assert (await api.post("/api/v1/sources", json=board(f"b{index}"), headers=user(ALICE))).status_code == 201
    sixth = await api.post("/api/v1/sources", json=board("b5"), headers=user(ALICE))
    assert sixth.status_code == 402
    problem = sixth.json()
    assert problem["type"].endswith("/plan_limit")
    assert (problem["plan"], problem["limit"]) == ("free", 5)


async def test_free_plan_polls_no_faster_than_its_floor(api: httpx.AsyncClient) -> None:
    created = (
        await api.post(
            "/api/v1/sources",
            json={**board("acme"), "poll_interval_seconds": 60, "min_poll_interval_seconds": 60},
            headers=user(ALICE),
        )
    ).json()
    assert created["min_poll_interval_seconds"] == 900  # raised to the free-plan floor
    assert created["poll_interval_seconds"] >= 900
    faster = await api.patch(
        f"/api/v1/sources/{created['id']}", json={"min_poll_interval_seconds": 120}, headers=user(ALICE)
    )
    assert faster.status_code == 402


async def test_free_workspaces_have_one_seat(api: httpx.AsyncClient) -> None:
    invite = await api.post("/api/v1/workspace/invitations", json={}, headers=user(ALICE))
    assert invite.status_code == 402


async def test_reevaluation_quota(redis_settings: Settings, ctx: AppContext) -> None:
    from tests.conftest import load_fixture  # noqa: PLC0415
    from tests.integration.test_pipeline import create_source, discover  # noqa: PLC0415

    source_id = await create_source(ctx)
    stored = await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    job_id = stored.jobs_to_evaluate[0]  # type: ignore[attr-defined]
    async with api_for(redis_settings, ctx) as client:
        bob = user(BOB)
        await client.get("/api/v1/me", headers=bob)  # personal free workspace (20 per day)
        # Bob must follow the board to see (and re-evaluate) its jobs.
        followed = await client.post("/api/v1/sources", json=board("acme"), headers=bob)
        assert followed.status_code == 201, followed.text
        statuses = [(await client.post(f"/api/v1/jobs/{job_id}/evaluate", headers=bob)).status_code for _ in range(21)]
    assert statuses[:20] == [202] * 20
    assert statuses[20] == 402


async def test_billing_overview(api: httpx.AsyncClient) -> None:
    await api.post("/api/v1/sources", json=board("acme"), headers=user(ALICE))
    overview = (await api.get("/api/v1/billing", headers=user(ALICE))).json()
    assert overview["plan"] == "free"
    assert overview["billing_enabled"] is True
    assert overview["purchasable"] == ["pro", "team"]
    assert overview["usage"] == {"followed_sources": 1, "members": 1}
    assert overview["limits"]["followed_sources"] == 5


@respx.mock
async def test_checkout_then_webhooks_drive_the_plan(api: httpx.AsyncClient, ctx: AppContext) -> None:
    route = respx.post(f"{RAZORPAY}/subscriptions").mock(
        return_value=httpx.Response(
            200, json={"id": "sub_alice", "status": "created", "short_url": "https://rzp.io/i/abc"}
        )
    )
    ws = await workspace_of(api, ALICE)
    started = await api.post("/api/v1/billing/checkout", json={"plan": "team"}, headers=user(ALICE))
    assert started.json() == {"checkout_url": "https://rzp.io/i/abc", "subscription_id": "sub_alice"}
    sent = json.loads(route.calls.last.request.content)
    assert sent["plan_id"] == "plan_team"
    assert sent["notes"] == {"workspace_id": ws, "plan": "team"}
    assert (await api.get("/api/v1/billing", headers=user(ALICE))).json()["plan"] == "free"  # not paid yet

    body, headers = webhook(
        "subscription.activated",
        subscription_id="sub_alice",
        status="active",
        plan_id="plan_team",
        workspace_id=ws,
        event_id="evt_activate",
    )
    assert (await api.post("/api/v1/webhooks/razorpay", content=body, headers=headers)).json() == {"status": "applied"}
    assert (await api.post("/api/v1/webhooks/razorpay", content=body, headers=headers)).json() == {
        "status": "duplicate"
    }
    assert (await api.get("/api/v1/me", headers=user(ALICE))).json()["workspace"]["plan"] == "team"
    assert (await api.post("/api/v1/workspace/invitations", json={}, headers=user(ALICE))).status_code == 201

    # Payment failures exhaust retries: paid limits survive the grace period, then drop.
    body, headers = webhook(
        "subscription.halted",
        subscription_id="sub_alice",
        status="halted",
        plan_id="plan_team",
        workspace_id=ws,
        current_end=datetime.now(tz=UTC),
    )
    await api.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert (await api.get("/api/v1/billing", headers=user(ALICE))).json()["plan"] == "team"
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        await session.execute(
            update(Workspace)
            .where(Workspace.id == uuid.UUID(ws))
            .values(grace_until=datetime.now(tz=UTC) - timedelta(seconds=1))
        )
    assert (await api.get("/api/v1/billing", headers=user(ALICE))).json()["plan"] == "free"


async def test_webhooks_are_authenticated_and_never_trust_notes(api: httpx.AsyncClient) -> None:
    ws = await workspace_of(api, ALICE)
    body, headers = webhook(
        "subscription.activated", subscription_id="sub_forged", status="active", plan_id="plan_team", workspace_id=ws
    )
    forged = await api.post(
        "/api/v1/webhooks/razorpay", content=body, headers={**headers, "x-razorpay-signature": "0" * 64}
    )
    assert forged.status_code == 401
    assert (await api.get("/api/v1/me", headers=user(ALICE))).json()["workspace"]["plan"] == "free"

    # Genuine event with a plan id we do not sell: the notes' "team" is ignored.
    body, headers = webhook(
        "subscription.activated", subscription_id="sub_odd", status="active", plan_id="plan_unknown", workspace_id=ws
    )
    await api.post("/api/v1/webhooks/razorpay", content=body, headers=headers)
    assert (await api.get("/api/v1/me", headers=user(ALICE))).json()["workspace"]["plan"] == "free"

    body, headers = webhook(
        "subscription.activated",
        subscription_id="sub_nobody",
        status="active",
        plan_id="plan_pro",
        workspace_id=str(uuid.uuid4()),
    )
    assert (await api.post("/api/v1/webhooks/razorpay", content=body, headers=headers)).json() == {"status": "ignored"}


async def test_only_owners_check_out_and_only_platform_admins_override(api: httpx.AsyncClient) -> None:
    ws = await workspace_of(api, BOB)
    assert (await api.post("/api/v1/billing/checkout", json={"plan": "pro"})).status_code == 403  # anonymous
    denied = await api.patch(f"/api/v1/admin/workspaces/{ws}/plan", json={"plan": "team"}, headers=user(ALICE))
    assert denied.status_code == 403
    admin = {"Authorization": f"Bearer {make_token(OWNER_ID)}"}
    granted = await api.patch(f"/api/v1/admin/workspaces/{ws}/plan", json={"plan": "team"}, headers=admin)
    assert granted.json() == {"plan": "team"}
    assert (await api.get("/api/v1/me", headers=user(BOB))).json()["workspace"]["plan"] == "team"


async def test_billing_disabled_without_configuration(settings: Settings, ctx: AppContext) -> None:
    async with api_for(settings, ctx) as client:
        assert (await client.get("/api/v1/billing", headers=user(ALICE))).json()["billing_enabled"] is False
        assert (
            await client.post("/api/v1/billing/checkout", json={"plan": "pro"}, headers=user(ALICE))
        ).status_code == 404
        assert (await client.post("/api/v1/webhooks/razorpay", content=b"{}")).status_code == 404
