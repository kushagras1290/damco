"""Discovery + evaluation services against a real database (no Temporal, no OpenAI)."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import uuid
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from jobpulse.db.models import DEFAULT_WORKSPACE_ID, EligibilityDecision, Job, JobVersion, MatchScore, RawSnapshot
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import workspace_scope
from jobpulse.repositories.jobs import JobFilters, JobRepository
from jobpulse.repositories.profiles import ProfileRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.repositories.tenancy import SubscriptionRepository
from jobpulse.seed import apply_seed, load_seed, start_polling
from jobpulse.services.context import AppContext
from jobpulse.services.evaluation import EvaluationService
from jobpulse.services.event_hub import EventHub
from jobpulse.services.events import EventType, publish
from jobpulse.services.ingestion import IngestionService
from jobpulse_core.contracts import JobRef, PollRecord
from jobpulse_core.domain.models import SourceDefinition, SourceKind
from tests.conftest import load_fixture

pytestmark = pytest.mark.integration

SEED_FILE = Path(__file__).resolve().parents[2] / "seed.yaml"
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"
DEFAULT = workspace_scope(DEFAULT_WORKSPACE_ID)


async def default_profile_id(ctx: AppContext) -> str:
    async with transaction(ctx.sessions, scope=DEFAULT) as session:
        return str((await ProfileRepository(session).get_or_create_primary()).id)


def ref_for(job_id: str, profile_id: str, content_hash: str | None) -> JobRef:
    return JobRef(
        job_id=job_id, workspace_id=str(DEFAULT_WORKSPACE_ID), profile_id=profile_id, content_hash=content_hash
    )


async def create_source(ctx: AppContext, name: str = "Acme Greenhouse", board: str = "acme") -> str:
    definition = SourceDefinition(
        kind=SourceKind.GREENHOUSE, company_name="Acme", company_domain="acme.io", board_token=board
    )
    async with transaction(ctx.sessions) as session:
        repo = SourceRepository(session)
        company = await repo.upsert_company(name="Acme", domain="acme.io")
        source = await repo.create(
            company_id=company.id,
            name=name,
            kind="greenhouse",
            config=definition.model_dump(mode="json"),
            poll_interval_seconds=900,
            min_poll_interval_seconds=300,
            max_poll_interval_seconds=3600,
        )
        source_id = source.id
    async with transaction(ctx.sessions, scope=DEFAULT) as session:
        await SubscriptionRepository(session).subscribe(source_id)  # as the API does on create
    return str(source_id)


async def discover(ctx: AppContext, source_id: str, payload: object, url: str = GREENHOUSE_URL) -> object:
    service = IngestionService(ctx)
    with respx.mock:
        respx.get(url).mock(return_value=httpx.Response(200, json=payload))
        fetched = await service.fetch(source_id)
    assert fetched.staging_key is not None
    normalized = await service.normalize(source_id, fetched.staging_key)
    return await service.store(source_id, normalized.normalized_key)


async def test_discovery_is_idempotent_and_versioned(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    board = load_fixture("greenhouse/board.json")

    first = await discover(ctx, source_id, board)
    assert first.new_jobs == 2
    assert len(first.jobs_to_evaluate) == 2

    again = await discover(ctx, source_id, board)
    assert again.new_jobs == 0
    assert again.unchanged_jobs == 2
    assert again.jobs_to_evaluate == []

    changed = copy.deepcopy(board)
    changed["jobs"][0]["content"] += "&lt;p&gt;Now with Kubernetes.&lt;/p&gt;"
    changed["jobs"] = changed["jobs"][:1] + changed["jobs"][2:]  # second job disappears
    third = await discover(ctx, source_id, changed)
    assert third.updated_jobs == 1
    assert third.closed_jobs == 1

    async with transaction(ctx.sessions) as session:
        versions = (await session.execute(select(func.max(JobVersion.version)))).scalar_one()
        snapshots = (
            await session.execute(select(func.count(RawSnapshot.id)).where(RawSnapshot.job_id.is_not(None)))
        ).scalar_one()
        closed = (await session.execute(select(func.count(Job.id)).where(Job.closed_at.is_not(None)))).scalar_one()
    assert versions == 2
    assert snapshots == 3
    assert closed == 1


async def test_cross_source_duplicates_are_linked(ctx: AppContext) -> None:
    board = load_fixture("greenhouse/board.json")
    primary = await create_source(ctx, "Primary")
    await discover(ctx, primary, board)
    mirror = await create_source(ctx, "Mirror", board="acme-mirror")
    outcome = await discover(
        ctx,
        mirror,
        board,
        url="https://boards-api.greenhouse.io/v1/boards/acme-mirror/jobs?content=true",
    )
    assert outcome.duplicate_jobs == 2
    assert outcome.jobs_to_evaluate == []


async def test_evaluation_persists_explainable_decisions(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    stored = await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    async with transaction(ctx.sessions, scope=DEFAULT) as session:
        profile = await ProfileRepository(session).get_or_create_primary()
        profile.skills = ["Python", "FastAPI", "PostgreSQL", "RAG", "LLM"]
        profile.target_roles = ["Senior AI Engineer"]
        profile.seniority = "senior"
        profile_id = str(profile.id)

    service = EvaluationService(ctx)
    results = {}
    for job_id in stored.jobs_to_evaluate:
        ref = ref_for(job_id, profile_id, stored.content_hashes[job_id])
        gate = await service.eligibility(ref, "wf-test")
        enriched = await service.enrich(ref, "wf-test")  # intelligence disabled -> passthrough
        assert enriched.detail == "intelligence disabled"
        ranked = await service.rank(ref, "wf-test") if gate.proceed else None
        notified = await service.notify(ref) if ranked and ranked.proceed else None
        results[job_id] = (gate, ranked, notified)

    eligible = [r for r in results.values() if r[0].eligible]
    rejected = [r for r in results.values() if not r[0].eligible]
    assert len(eligible) == 1  # worldwide remote AI role
    assert len(rejected) == 1  # US-resident, 10+ years staff role
    ranked = eligible[0][1]
    assert ranked is not None
    assert ranked.score is not None

    async with transaction(ctx.sessions, scope=DEFAULT) as session:
        decisions = (await session.execute(select(EligibilityDecision))).scalars().all()
        scores = (await session.execute(select(MatchScore))).scalars().all()
        jobs, total = await JobRepository(session).search(
            JobFilters(eligibility_status="eligible"), profile_id=uuid.UUID(profile_id), limit=10, offset=0
        )
    assert len(decisions) == 2
    failed = next(d for d in decisions if d.status == "ineligible")
    assert {r["rule"] for r in failed.rules if r["outcome"] == "fail"} >= {"excluded_region", "location"}
    assert len(scores) == 1
    assert {c["name"] for c in scores[0].components} >= {"skill_match", "freshness"}
    assert total == 1
    assert jobs[0].match_score == scores[0].final_score


async def test_search_and_fuzzy_title(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    profile_id = uuid.UUID(await default_profile_id(ctx))
    async with transaction(ctx.sessions, scope=DEFAULT) as session:
        repo = JobRepository(session)
        fts, fts_total = await repo.search(JobFilters(query="FastAPI"), profile_id=profile_id, limit=10, offset=0)
        fuzzy, _ = await repo.search(
            JobFilters(query="senior artificial intelligence enginer"), profile_id=profile_id, limit=10, offset=0
        )
        job = fts[0].job
        similar = await repo.similar_titles(job)
    assert fts_total == 1
    assert fts[0].job.title.startswith("Senior AI Engineer")
    assert len(fuzzy) == 1
    assert isinstance(similar, list)


async def test_record_poll_clamps_interval_and_opens_circuit(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    service = IngestionService(ctx)
    await service.record_poll(
        PollRecord(
            source_id=source_id,
            success=False,
            new_jobs=0,
            next_interval_seconds=99_999,
            circuit_open_seconds=600,
            error="boom",
        ),
    )
    schedule = await service.schedule(source_id)
    assert schedule.poll_interval_seconds == 3600
    assert schedule.consecutive_failures == 1
    assert 0 < schedule.circuit_open_remaining_seconds <= 600


async def test_snapshot_is_replayable(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    stored = await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    job_id = uuid.UUID(stored.jobs_to_evaluate[0])
    async with transaction(ctx.sessions) as session:
        snapshot = await JobRepository(session).latest_snapshot(job_id)
    assert snapshot is not None
    payload = json.loads(await ctx.store.get(snapshot.snapshot_key))
    assert payload["external_id"] in {"4012345", "4012346"}


async def test_seed_is_idempotent(ctx: AppContext) -> None:
    seed = load_seed(SEED_FILE)
    first = await apply_seed(ctx, seed)
    second = await apply_seed(ctx, seed)
    assert first.profile_updated
    assert len(first.created_source_ids) == len(seed.sources)
    assert second.created_source_ids == []
    assert sorted(second.enabled_source_ids) == sorted(first.created_source_ids)  # still ensured
    async with transaction(ctx.sessions, scope=DEFAULT) as session:
        profile = await ProfileRepository(session).get_or_create_primary()
    assert "Python" in profile.skills


async def test_seed_polling_start_is_best_effort(ctx: AppContext) -> None:
    result = await apply_seed(ctx, load_seed(SEED_FILE))
    offline = replace(ctx, settings=ctx.settings.model_copy(update={"temporal_address": "127.0.0.1:1"}))
    assert await start_polling(offline, []) == 0
    assert await start_polling(offline, result.created_source_ids) == 0  # deferred to worker boot, no crash


async def test_unchanged_listing_snapshot_is_stored_once(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    board = load_fixture("greenhouse/board.json")
    await discover(ctx, source_id, board)
    await discover(ctx, source_id, board)  # identical payload (no ETag support upstream)
    async with transaction(ctx.sessions) as session:
        pages = (
            await session.execute(select(func.count(RawSnapshot.id)).where(RawSnapshot.job_id.is_(None)))
        ).scalar_one()
    assert pages == 1


async def test_suspicious_shrink_does_not_mass_close(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    base = load_fixture("greenhouse/board.json")["jobs"][0]
    many = {"jobs": [{**base, "id": 5_000_000 + i, "title": f"Engineer {i}"} for i in range(20)]}
    await discover(ctx, source_id, many)
    truncated = {"jobs": many["jobs"][:3]}  # upstream glitch: 85% of the board missing
    outcome = await discover(ctx, source_id, truncated)
    assert outcome.closed_jobs == 0
    async with transaction(ctx.sessions) as session:
        assert await JobRepository(session).open_count(uuid.UUID(source_id)) == 20


async def test_llm_daily_limit_degrades_enrichment(ctx: AppContext) -> None:
    source_id = await create_source(ctx)
    stored = await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
    job_id = stored.jobs_to_evaluate[0]
    ref = ref_for(job_id, await default_profile_id(ctx), stored.content_hashes[job_id])

    class ExplodingIntelligence:
        """Any call would mean the budget guard failed."""

        async def extract(self, *_: object, **__: object) -> None:
            raise AssertionError("LLM must not be called once the daily limit is reached")

        async def aclose(self) -> None:
            return None

    ctx.intelligence = ExplodingIntelligence()  # type: ignore[assignment]
    ctx.settings = ctx.settings.model_copy(update={"openai_daily_request_limit": 0})
    service = EvaluationService(ctx)
    await service.eligibility(ref, "wf")
    result = await service.enrich(ref, "wf")
    assert result.proceed
    assert result.detail == "llm daily request limit reached"


async def _collect(hub: EventHub, until_type: str) -> list[dict[str, object]]:
    received: list[dict[str, object]] = []
    async with hub.subscribe() as subscription:
        async with asyncio.timeout(10):
            while True:
                event = json.loads(await subscription.queue.get())
                received.append(event)
                if event["type"] == until_type:
                    return received


async def test_events_are_delivered_only_after_commit(ctx: AppContext) -> None:
    hub = EventHub(ctx.settings.realtime_dsn, max_clients=5, queue_size=50)
    await hub.start()
    try:
        async with asyncio.timeout(10):
            await hub.wait_connected()
        collector = asyncio.create_task(_collect(hub, "job.matched"))
        await asyncio.sleep(0.1)
        with contextlib.suppress(RuntimeError):
            async with transaction(ctx.sessions) as session:
                await publish(session, EventType.JOB_EVALUATED, {"job_id": "rolled-back"})
                raise RuntimeError  # rollback: must never be delivered
        async with transaction(ctx.sessions) as session:
            await publish(session, EventType.JOB_MATCHED, {"job_id": "committed", "score": 0.91})
        events = await collector
        assert [e["data"]["job_id"] for e in events] == ["committed"]  # type: ignore[index]
    finally:
        await hub.stop()


async def test_discovery_publishes_realtime_events(ctx: AppContext) -> None:
    hub = EventHub(ctx.settings.realtime_dsn, max_clients=5, queue_size=100)
    await hub.start()
    try:
        async with asyncio.timeout(10):
            await hub.wait_connected()
        source_id = await create_source(ctx)
        collector = asyncio.create_task(_collect(hub, "jobs.discovered"))
        await asyncio.sleep(0.1)
        await discover(ctx, source_id, load_fixture("greenhouse/board.json"))
        events = await collector
    finally:
        await hub.stop()
    stages = [e["data"].get("stage") for e in events]  # type: ignore[union-attr]
    assert stages[:2] == ["fetching", "fetched"]
    discovered = events[-1]["data"]
    assert discovered["new"] == 2  # type: ignore[index]
    assert {job["title"] for job in discovered["jobs"]} >= {"Staff Backend Engineer"}  # type: ignore[index]
