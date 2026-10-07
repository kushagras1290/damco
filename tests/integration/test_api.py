"""REST API end-to-end through the ASGI app (auth, roles, validation, explainability)."""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest

from jobpulse.api.deps import get_ctx, get_temporal
from jobpulse.core.config import Settings
from jobpulse.db.models import DEFAULT_WORKSPACE_ID
from jobpulse.main import create_app
from jobpulse.services.context import AppContext
from tests.auth_helpers import OWNER_ID, SIGNING_KEY, VISITOR_ID, make_token, other_signing_key
from tests.conftest import load_fixture
from tests.integration.test_pipeline import create_source, discover

pytestmark = pytest.mark.integration


@dataclass
class FakeHandle:
    id: str

    async def signal(self, *_: Any) -> None:
        return None


@dataclass
class FakeTemporal:
    started: list[dict[str, Any]] = field(default_factory=list)

    async def start_workflow(self, workflow: str, arg: Any, **kwargs: Any) -> FakeHandle:
        self.started.append({"workflow": workflow, "arg": arg, **kwargs})
        return FakeHandle(id=kwargs["id"])

    def get_workflow_handle(self, workflow_id: str) -> FakeHandle:
        return FakeHandle(id=workflow_id)


@pytest.fixture
def temporal() -> FakeTemporal:
    return FakeTemporal()


@pytest.fixture
async def api(settings: Settings, ctx: AppContext, temporal: FakeTemporal) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: ctx
    app.dependency_overrides[get_temporal] = lambda: temporal
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


OWNER = {"Authorization": f"Bearer {make_token(OWNER_ID)}"}
DEMO = {"Authorization": f"Bearer {make_token(VISITOR_ID, login='visitor')}"}


async def test_health_and_metrics(api: httpx.AsyncClient) -> None:
    assert (await api.get("/health/live")).json() == {"status": "ok"}
    assert (await api.get("/health/ready")).status_code == 200
    metrics = await api.get("/metrics")
    assert "jobs_discovered_total" in metrics.text


async def test_security_headers_and_request_id(api: httpx.AsyncClient) -> None:
    response = await api.get("/api/v1/jobs", headers={"X-Request-ID": "abcdef123456"})
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "abcdef123456"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


async def test_public_demo_is_read_only(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/sources")).status_code == 200
    body = {
        "kind": "greenhouse",
        "name": "x",
        "company_name": "Acme",
        "company_domain": "acme.io",
        "board_token": "acme",
    }
    anonymous = await api.post("/api/v1/sources", json=body)
    assert anonymous.status_code == 403  # anonymous visitors are viewers of the demo workspace
    assert anonymous.headers["content-type"].startswith("application/problem+json")
    assert (await api.get("/api/v1/me")).json()["role"] == "viewer"


def _unsigned_alg_none_token() -> str:
    """Classic `alg: none` forgery, built at runtime (a literal JWT would trip secret scanners)."""

    def b64(data: dict[str, str]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{b64({'alg': 'none', 'kid': 'test-kid'})}.{b64({'sub': f'github:{OWNER_ID}'})}."


def _hs256_with_public_key() -> str:
    """Classic alg-confusion attempt: HMAC-sign using the *public* key bytes as secret."""
    public_raw = SIGNING_KEY.public_key().public_bytes_raw()
    return make_token(OWNER_ID, key=public_raw, algorithm="HS256")


@pytest.mark.parametrize(
    "token",
    [
        pytest.param(make_token(OWNER_ID, key=other_signing_key()), id="wrong-key"),
        pytest.param(make_token(OWNER_ID, kid="unknown-kid"), id="unknown-kid"),
        pytest.param(_hs256_with_public_key(), id="alg-confusion-hs256"),
        pytest.param(make_token(OWNER_ID, exp=1, iat=0, nbf=0), id="expired"),
        pytest.param(make_token(OWNER_ID, aud="someone-else"), id="wrong-audience"),
        pytest.param(make_token(OWNER_ID, iss="evil"), id="wrong-issuer"),
        pytest.param(make_token(OWNER_ID, lifetime=timedelta(hours=8)), id="lifetime-too-long"),
        pytest.param(make_token(OWNER_ID, sub="octocat"), id="login-as-subject"),
        pytest.param(make_token(OWNER_ID, jti=None), id="missing-jti"),
        pytest.param(_unsigned_alg_none_token(), id="alg-none"),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
async def test_invalid_tokens_rejected(api: httpx.AsyncClient, token: str) -> None:
    response = await api.get("/api/v1/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_role_and_workspace_claims_grant_nothing(api: httpx.AsyncClient) -> None:
    # A signed-in stranger forges an owner role claim and selects the Default workspace.
    token = make_token(VISITOR_ID, login="visitor", role="OWNER", wid=str(DEFAULT_WORKSPACE_ID))
    forged = {"Authorization": f"Bearer {token}"}
    me = (await api.get("/api/v1/me", headers=forged)).json()
    assert me["platform_admin"] is False
    assert me["workspace"]["id"] != str(DEFAULT_WORKSPACE_ID)  # not a member: falls back to their own
    assert me["workspace"]["personal"] is True
    assert [w["id"] for w in me["workspaces"]] == [me["workspace"]["id"]]
    body = {"kind": "lever", "name": "x", "company_name": "X", "company_domain": "x.io", "board_token": "x"}
    assert (await api.post("/api/v1/sources", json=body, headers=forged)).status_code == 201  # their workspace
    owners_view = (await api.get("/api/v1/sources", headers=OWNER)).json()
    assert owners_view["total"] == 0  # nothing leaked into the Default workspace


async def test_identity_is_the_immutable_id_not_the_login(api: httpx.AsyncClient) -> None:
    # A different account using the owner's former login gains nothing.
    squatter = {"Authorization": f"Bearer {make_token(VISITOR_ID, login='octocat')}"}
    renamed_owner = {"Authorization": f"Bearer {make_token(OWNER_ID, login='new-name')}"}
    squatter_me = (await api.get("/api/v1/me", headers=squatter)).json()
    owner_me = (await api.get("/api/v1/me", headers=renamed_owner)).json()
    assert squatter_me["platform_admin"] is False
    assert squatter_me["workspace"]["id"] != str(DEFAULT_WORKSPACE_ID)
    assert owner_me["platform_admin"] is True
    assert owner_me["workspace"]["id"] == str(DEFAULT_WORKSPACE_ID)
    assert owner_me["role"] == "owner"


async def test_me(api: httpx.AsyncClient) -> None:
    me = (await api.get("/api/v1/me", headers=OWNER)).json()
    assert me["subject"] == f"github:{OWNER_ID}"
    assert me["login"] == "octocat"
    assert me["authenticated"] is True
    assert me["platform_admin"] is True
    assert me["role"] == "owner"
    assert me["workspace"]["id"] == str(DEFAULT_WORKSPACE_ID)
    assert me["user_id"]
    anonymous = (await api.get("/api/v1/me")).json()
    assert anonymous["authenticated"] is False
    assert anonymous["user_id"] is None


async def test_owner_creates_source_and_polling_starts(api: httpx.AsyncClient, temporal: FakeTemporal) -> None:
    body = {
        "kind": "lever",
        "name": "Globex Lever",
        "company_name": "Globex",
        "company_domain": "globex.com",
        "board_token": "globex",
    }
    created = await api.post("/api/v1/sources", json=body, headers=OWNER)
    assert created.status_code == 201, created.text
    source = created.json()
    assert source["company_domain"] == "globex.com"
    assert temporal.started
    assert temporal.started[0]["id"] == f"source-polling-{source['id']}"

    duplicate = await api.post("/api/v1/sources", json=body, headers=OWNER)
    assert duplicate.status_code == 409

    sync = await api.post(f"/api/v1/sources/{source['id']}/sync", headers=OWNER)
    assert sync.status_code == 202
    assert temporal.started[-1]["start_signal"] == "poll_now"

    disabled = await api.patch(f"/api/v1/sources/{source['id']}", json={"enabled": False}, headers=OWNER)
    assert disabled.json()["enabled"] is False
    assert (await api.post(f"/api/v1/sources/{source['id']}/sync", headers=OWNER)).status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "rss", "name": "ssrf", "company_name": "x", "company_domain": "x.io", "url": "https://127.0.0.1/feed"},
        {
            "kind": "rss",
            "name": "http",
            "company_name": "x",
            "company_domain": "x.io",
            "url": "http://example.com/feed",
        },
        {"kind": "greenhouse", "name": "notoken", "company_name": "x", "company_domain": "x.io"},
        {
            "kind": "greenhouse",
            "name": "inj",
            "company_name": "x",
            "company_domain": "x.io",
            "board_token": "../../etc",
        },
        {
            "kind": "greenhouse",
            "name": "extra",
            "company_name": "x",
            "company_domain": "x.io",
            "board_token": "a",
            "admin": True,
        },
    ],
)
async def test_source_validation(api: httpx.AsyncClient, body: dict[str, Any]) -> None:
    response = await api.post("/api/v1/sources", json=body, headers=OWNER)
    assert response.status_code == 422, response.text


async def test_profile_update_and_ssrf_webhook(api: httpx.AsyncClient) -> None:
    profile = (await api.get("/api/v1/profile")).json()
    assert profile["policy"]["experience"] == {"min": 3.0, "max": 6.0}

    updated = await api.patch(
        "/api/v1/profile",
        json={"skills": ["Python", "python", " RAG "], "years_experience": 5, "notify_min_score": 0.6},
        headers=OWNER,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["skills"] == ["Python", "python", "RAG"]

    blocked = await api.patch("/api/v1/profile", json={"webhook_url": "https://169.254.169.254/hook"}, headers=OWNER)
    assert blocked.status_code == 422


async def test_job_listing_and_explainable_detail(api: httpx.AsyncClient, ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    await discover(ctx, source_id, load_fixture("greenhouse/board.json"))

    listing = await api.get("/api/v1/jobs", params={"q": "FastAPI"})
    assert listing.status_code == 200
    page = listing.json()
    assert page["total"] == 1
    job_id = page["items"][0]["id"]

    detail = (await api.get(f"/api/v1/jobs/{job_id}")).json()
    assert detail["snapshot"]["content_type"] == "application/json"
    assert detail["versions"][0]["version"] == 1
    assert "<script>" not in detail["description_html"]

    snapshot = await api.get(f"/api/v1/jobs/{job_id}/snapshot")
    assert "4012345" in snapshot.json()["content"]

    application = await api.post(f"/api/v1/jobs/{job_id}/applications", json={"status": "applied"}, headers=OWNER)
    assert application.status_code == 201
    apps = (await api.get("/api/v1/applications")).json()
    assert apps["items"][0]["status"] == "applied"

    rerun = await api.post(f"/api/v1/jobs/{job_id}/evaluate", headers=OWNER)
    assert rerun.status_code == 202

    dashboard = (await api.get("/api/v1/dashboard")).json()
    assert dashboard["sources_total"] == 1


async def test_validation_and_not_found(api: httpx.AsyncClient) -> None:
    assert (await api.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await api.get("/api/v1/jobs", params={"limit": 1000})).status_code == 422
    bad = await api.get("/api/v1/jobs/not-a-uuid")
    assert bad.status_code == 422
    assert bad.json()["errors"]


async def test_body_size_limit(api: httpx.AsyncClient) -> None:
    response = await api.patch(
        "/api/v1/profile", content=b"x" * (300 * 1024), headers={**OWNER, "content-type": "application/json"}
    )
    assert response.status_code == 413


async def test_rate_limit_is_keyed_by_verified_identity(settings: Settings, ctx: AppContext) -> None:
    limited = settings.model_copy(update={"rate_limit_anonymous": 2, "rate_limit_authenticated": 3})
    app = create_app(limited)
    app.dependency_overrides[get_ctx] = lambda: ctx
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            anonymous = [await client.get("/api/v1/me") for _ in range(3)]
            owner = [(await client.get("/api/v1/me", headers=OWNER)).status_code for _ in range(4)]
            forged = make_token(OWNER_ID, key=other_signing_key())
            bad = (await client.get("/api/v1/me", headers={"Authorization": f"Bearer {forged}"})).status_code
    assert [r.status_code for r in anonymous] == [200, 200, 429]
    assert anonymous[0].headers["ratelimit-limit"] == "2"
    assert anonymous[0].headers["ratelimit-remaining"] == "1"
    assert int(anonymous[2].headers["retry-after"]) >= 1
    assert owner == [200, 200, 200, 429]  # own (higher) bucket, separate from the IP's anonymous traffic
    assert bad == 429  # unverifiable token falls back to the (exhausted) IP bucket


async def test_writes_have_a_tighter_bucket(settings: Settings, ctx: AppContext) -> None:
    app = create_app(settings.model_copy(update={"rate_limit_writes": 1}))
    app.dependency_overrides[get_ctx] = lambda: ctx
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first = await client.patch("/api/v1/profile", json={"display_name": "one"}, headers=OWNER)
            second = await client.patch("/api/v1/profile", json={"display_name": "two"}, headers=OWNER)
            read = await client.get("/api/v1/profile", headers=OWNER)
    assert first.status_code == 200
    assert second.status_code == 429
    assert read.status_code == 200  # reads are unaffected by the write bucket
