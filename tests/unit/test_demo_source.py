"""The demo board must release incrementally and showcase every eligibility path."""

from __future__ import annotations

import pytest

from jobpulse_core.domain.models import EligibilityPolicy, SourceCheckpoint, SourceDefinition, SourceKind
from jobpulse_core.eligibility import RuleName, RuleOutcome, evaluate
from jobpulse_core.ingestion.http import HttpClientConfig, SafeHttpClient
from jobpulse_core.ingestion.normalize import normalize_job
from jobpulse_core.sources import build_source
from jobpulse_core.sources.demo import INITIAL_RELEASE, RELEASE_PER_POLL, demo_postings

DEMO = SourceDefinition(kind=SourceKind.DEMO, company_name="Demo board", company_domain="demo.example")


async def test_releases_incrementally_and_keeps_released_jobs_open() -> None:
    async with SafeHttpClient(HttpClientConfig()) as http:
        source = build_source(DEMO, http)
        first = await source.discover(SourceCheckpoint())
        second = await source.discover(first.checkpoint)
    assert len(first.jobs) == INITIAL_RELEASE
    assert len(second.jobs) == INITIAL_RELEASE + RELEASE_PER_POLL
    assert [j.external_id for j in second.jobs[:INITIAL_RELEASE]] == [j.external_id for j in first.jobs]


async def test_release_stops_at_the_end_of_the_board() -> None:
    total = len(demo_postings())
    async with SafeHttpClient(HttpClientConfig()) as http:
        result = await build_source(DEMO, http).discover(SourceCheckpoint(cursor={"demo_released": total}))
    assert len(result.jobs) == total


def test_companies_are_fictional_reserved_domains() -> None:
    assert all(posting["domain"].endswith(".example") for posting in demo_postings())


@pytest.mark.parametrize(
    ("posting_id", "rule", "outcome"),
    [
        ("nw-001", None, None),  # worldwide remote AI role -> eligible
        ("qz-002", None, None),  # remote India -> eligible
        ("lm-003", RuleName.EXCLUDED_REGION, RuleOutcome.FAIL),  # must reside in the US
        ("tp-004", RuleName.LOCATION, RuleOutcome.FAIL),  # EU only
        ("bf-005", RuleName.WORK_MODEL, RuleOutcome.FAIL),  # on-site
        ("ch-006", RuleName.EXPERIENCE, RuleOutcome.FAIL),  # 12+ years
        ("mh-007", RuleName.EXPERIENCE, RuleOutcome.FAIL),  # 0-1 years
        ("pf-008", RuleName.TIMEZONE, RuleOutcome.FAIL),  # must work PST hours
        ("ks-009", None, None),  # "Preferred: 8+ years" must not reject
        ("mh-017", RuleName.EXCLUDED_REGION, RuleOutcome.FAIL),  # Canada-only
        ("mh-027", RuleName.EXCLUDED_REGION, RuleOutcome.FAIL),  # US security clearance
    ],
)
def test_each_showcase_posting_gets_the_intended_verdict(
    posting_id: str, rule: RuleName | None, outcome: RuleOutcome | None
) -> None:
    from datetime import UTC, datetime  # noqa: PLC0415 - local to the parametrized helper

    from jobpulse_core.sources.demo import DemoSource  # noqa: PLC0415

    index = next(i for i, p in enumerate(demo_postings()) if p["id"] == posting_id)
    raw = DemoSource._to_raw(demo_postings()[index], datetime.now(tz=UTC), index, index + 1)
    result = evaluate(normalize_job(raw), EligibilityPolicy())
    if rule is None:
        assert result.eligible, [r.to_dict() for r in result.rules]
    else:
        assert result.rule(rule).outcome is outcome
        assert not result.eligible
