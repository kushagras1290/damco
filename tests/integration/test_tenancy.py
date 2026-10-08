"""Multi-tenant isolation: two workspaces share the job catalogue but never see each other.

These run against real PostgreSQL with row-level security, as the non-owner app role.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from jobpulse.api.deps import get_ctx
from jobpulse.core.config import Settings
from jobpulse.db.models import Application, EligibilityDecision, Profile, ProfileJob
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import NO_SCOPE, SYSTEM_SCOPE, TenantScope, workspace_scope
from jobpulse.main import create_app
from jobpulse.repositories.jobs import JobFilters, JobRepository
from jobpulse.repositories.tenancy import SubscriptionRepository, WorkspaceRepository
from jobpulse.services.context import AppContext
from jobpulse.services.evaluation import EvaluationService, ProfileNotFoundError
from jobpulse.services.ingestion import IngestionService
from jobpulse_core.contracts import JobRef
from jobpulse_core.domain.models import EligibilityPolicy, RemotePolicy
from tests.auth_helpers import make_token
from tests.conftest import load_fixture
from tests.integration.test_pipeline import create_source, discover

pytestmark = pytest.mark.integration

ALICE_WS = uuid.UUID("00000000-0000-7000-8000-00000000a11c")
BOB_WS = uuid.UUID("00000000-0000-7000-8000-000000000b0b")


async def make_workspace(ctx: AppContext, workspace_id: uuid.UUID, slug: str, policy: EligibilityPolicy) -> uuid.UUID:
    """Workspace + one profile with its own eligibility policy; returns the profile id."""
    async with transaction(ctx.sessions, scope=workspace_scope(workspace_id)) as session:
        await WorkspaceRepository(session).ensure(workspace_id=workspace_id, name=slug, slug=slug, plan="free")
        profile = Profile(
            display_name=slug,
            skills=["Python", "FastAPI", "LLM"],
            target_roles=["Senior AI Engineer"],
            seniority="senior",
            years_experience=Decimal(6),
            policy=policy.model_dump(mode="json"),
        )
        session.add(profile)
        await session.flush()
        return profile.id


async def two_tenants_sharing_a_source(ctx: AppContext) -> tuple[uuid.UUID, uuid.UUID, list[str], dict[str, str]]:
    alice = await make_workspace(ctx, ALICE_WS, "alice", EligibilityPolicy())
    # Bob only accepts on-site roles in Berlin: the same remote job is ineligible for him.
    bob = await make_workspace(
        ctx, BOB_WS, "bob", EligibilityPolicy(allowed_locations=["Germany"], allowed_work_models=[RemotePolicy.ONSITE])
    )
    source_id = await create_source(ctx)
    for workspace in (ALICE_WS, BOB_WS):
        async with transaction(ctx.sessions, scope=workspace_scope(workspace)) as session:
            await SubscriptionRepository(session).subscribe(uuid.UUID(source_id))
    stored = await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    return alice, bob, stored.jobs_to_evaluate, stored.content_hashes  # type: ignore[attr-defined]


async def insert_in_scope(ctx: AppContext, scope: TenantScope, row: Profile | Application) -> None:
    async with transaction(ctx.sessions, scope=scope) as session:
        session.add(row)
        await session.flush()


async def evaluate(ctx: AppContext, workspace: uuid.UUID, profile: uuid.UUID, jobs: list[str]) -> None:
    service = EvaluationService(ctx)
    for job_id in jobs:
        ref = JobRef(job_id=job_id, workspace_id=str(workspace), profile_id=str(profile))
        gate = await service.eligibility(ref, "wf-tenancy")
        if gate.proceed:
            await service.rank(ref, "wf-tenancy")


async def test_runs_as_a_role_that_cannot_bypass_rls(ctx: AppContext) -> None:
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        row = (
            await session.execute(
                text("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
    assert tuple(row) == ("jobpulse_app", False, False)


async def test_fan_out_targets_every_subscribed_profile(ctx: AppContext) -> None:
    alice, bob, _, _ = await two_tenants_sharing_a_source(ctx)
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        source_id = (await session.execute(text("SELECT id FROM sources"))).scalar_one()
    targets = await IngestionService(ctx).evaluation_targets(str(source_id))
    assert {(t.workspace_id, t.profile_id) for t in targets.targets} == {
        (str(ALICE_WS), str(alice)),
        (str(BOB_WS), str(bob)),
    }


async def test_same_job_different_verdicts_per_workspace(ctx: AppContext) -> None:
    alice, bob, jobs, _ = await two_tenants_sharing_a_source(ctx)
    await evaluate(ctx, ALICE_WS, alice, jobs)
    await evaluate(ctx, BOB_WS, bob, jobs)

    async with transaction(ctx.sessions, scope=workspace_scope(ALICE_WS)) as session:
        alice_views, _ = await JobRepository(session).search(
            JobFilters(eligibility_status="eligible"), profile_id=alice, limit=10, offset=0
        )
        alice_rows = (await session.execute(select(ProfileJob))).scalars().all()
        alice_decisions = (await session.execute(select(EligibilityDecision))).scalars().all()
    async with transaction(ctx.sessions, scope=workspace_scope(BOB_WS)) as session:
        bob_views, _ = await JobRepository(session).search(
            JobFilters(eligibility_status="eligible"), profile_id=bob, limit=10, offset=0
        )
        bob_rows = (await session.execute(select(ProfileJob))).scalars().all()

    assert len(alice_views) == 1  # the worldwide-remote AI role
    assert bob_views == []  # Bob's Berlin-only policy rejects everything on this board
    assert {row.profile_id for row in alice_rows} == {alice}  # RLS: Alice never sees Bob's verdicts
    assert {row.profile_id for row in bob_rows} == {bob}
    assert all(decision.workspace_id == ALICE_WS for decision in alice_decisions)


async def test_no_scope_fails_closed(ctx: AppContext) -> None:
    await make_workspace(ctx, ALICE_WS, "alice", EligibilityPolicy())
    async with transaction(ctx.sessions, scope=NO_SCOPE) as session:
        assert (await session.execute(select(Profile))).scalars().all() == []
    with pytest.raises(DBAPIError, match="row-level security"):
        await insert_in_scope(ctx, NO_SCOPE, Profile(display_name="orphan", policy={}))


async def test_cannot_write_into_another_workspace(ctx: AppContext) -> None:
    alice, bob, jobs, _ = await two_tenants_sharing_a_source(ctx)
    # Explicitly targeting Bob's workspace from Alice's scope is rejected by WITH CHECK.
    forged = Application(job_id=uuid.UUID(jobs[0]), profile_id=bob, workspace_id=BOB_WS)
    with pytest.raises(DBAPIError, match="row-level security"):
        await insert_in_scope(ctx, workspace_scope(ALICE_WS), forged)
    async with transaction(ctx.sessions, scope=workspace_scope(ALICE_WS)) as session:
        assert await session.get(Profile, bob) is None  # Bob's profile is invisible to Alice
        assert await session.get(Profile, alice) is not None


async def test_evaluation_cannot_use_a_profile_from_another_workspace(ctx: AppContext) -> None:
    _, bob, jobs, _ = await two_tenants_sharing_a_source(ctx)
    forged = JobRef(job_id=jobs[0], workspace_id=str(ALICE_WS), profile_id=str(bob))
    with pytest.raises(ProfileNotFoundError):
        await EvaluationService(ctx).eligibility(forged, "wf-forged")


async def test_api_only_exposes_its_own_workspace(settings: Settings, ctx: AppContext) -> None:
    _, bob, jobs, _ = await two_tenants_sharing_a_source(ctx)
    await evaluate(ctx, BOB_WS, bob, jobs)
    async with transaction(ctx.sessions, scope=workspace_scope(BOB_WS)) as session:
        session.add(Application(job_id=uuid.UUID(jobs[0]), profile_id=bob, status="applied"))

    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: ctx
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            applications = (await client.get("/api/v1/applications")).json()
            profile = (await client.get("/api/v1/profile")).json()
            decisions = (await client.get("/api/v1/decisions")).json()
            sources = (await client.get("/api/v1/sources")).json()
    assert applications["total"] == 0  # Bob's application is not visible in the Default workspace
    assert profile["display_name"] != "bob"
    assert decisions["total"] == 0
    assert sources["total"] == 1  # the catalogue source is shared (Default follows it too) ...
    assert sources["items"][0]["open_jobs"] >= 1  # ... but none of Bob's verdicts or applications leak


async def test_workspaces_only_see_catalogue_from_boards_they_follow(settings: Settings, ctx: AppContext) -> None:
    source_id = await create_source(ctx)  # followed by the Default workspace only
    stored = await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    job_id = stored.jobs_to_evaluate[0]  # type: ignore[attr-defined]
    app = create_app(settings)
    app.dependency_overrides[get_ctx] = lambda: ctx
    stranger = {"Authorization": f"Bearer {make_token(7777, login='stranger')}"}  # gets a fresh personal workspace
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            follower_jobs = (await client.get("/api/v1/jobs")).json()  # anonymous = Default workspace
            stranger_jobs = (await client.get("/api/v1/jobs", headers=stranger)).json()
            stranger_dashboard = (await client.get("/api/v1/dashboard", headers=stranger)).json()
            stranger_detail = await client.get(f"/api/v1/jobs/{job_id}", headers=stranger)
            stranger_snapshot = await client.get(f"/api/v1/jobs/{job_id}/snapshot", headers=stranger)
    assert follower_jobs["total"] >= 1
    assert stranger_jobs["total"] == 0
    assert sum(stranger_dashboard["jobs_by_status"].values()) == 0
    assert stranger_dashboard["discovered_per_day"] == []
    assert stranger_detail.status_code == 404
    assert stranger_snapshot.status_code == 404
