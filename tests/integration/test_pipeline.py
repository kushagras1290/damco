"""Discovery + evaluation services against a real database (no Temporal, no OpenAI)."""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import func, select

from jobpulse.db.models import EligibilityDecision, Job, JobVersion, MatchScore, RawSnapshot
from jobpulse.db.session import transaction
from jobpulse.repositories.jobs import JobFilters, JobRepository
from jobpulse.repositories.profiles import ProfileRepository
from jobpulse.repositories.sources import SourceRepository
from jobpulse.seed import apply_seed, load_seed
from jobpulse.services.context import AppContext
from jobpulse.services.evaluation import EvaluationService
from jobpulse.services.ingestion import IngestionService
from jobpulse_core.contracts import JobRef, PollRecord
from jobpulse_core.domain.models import SourceDefinition, SourceKind
from tests.conftest import load_fixture

pytestmark = pytest.mark.integration

SEED_FILE = Path(__file__).resolve().parents[2] / "seed.yaml"
GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true"


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
        return str(source.id)


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
    async with transaction(ctx.sessions) as session:
        profile = await ProfileRepository(session).get_or_create_primary()
        profile.skills = ["Python", "FastAPI", "PostgreSQL", "RAG", "LLM"]
        profile.target_roles = ["Senior AI Engineer"]
        profile.seniority = "senior"

    service = EvaluationService(ctx)
    results = {}
    for job_id in stored.jobs_to_evaluate:
        ref = JobRef(job_id=job_id, content_hash=stored.content_hashes[job_id])
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

    async with transaction(ctx.sessions) as session:
        decisions = (await session.execute(select(EligibilityDecision))).scalars().all()
        scores = (await session.execute(select(MatchScore))).scalars().all()
        jobs, total = await JobRepository(session).search(JobFilters(eligibility_status="eligible"), limit=10, offset=0)
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
    async with transaction(ctx.sessions) as session:
        repo = JobRepository(session)
        fts, fts_total = await repo.search(JobFilters(query="FastAPI"), limit=10, offset=0)
        fuzzy, _ = await repo.search(JobFilters(query="senior artificial intelligence enginer"), limit=10, offset=0)
        job = fts[0]
        similar = await repo.similar_titles(job)
    assert fts_total == 1
    assert fts[0].title.startswith("Senior AI Engineer")
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
    assert first == (True, len(seed.sources))
    assert second == (True, 0)
    async with transaction(ctx.sessions) as session:
        profile = await ProfileRepository(session).get_or_create_primary()
    assert "Python" in profile.skills
