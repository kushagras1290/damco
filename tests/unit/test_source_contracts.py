"""Parser contract tests against frozen fixtures: adapters must fail loudly on drift."""

from __future__ import annotations

import httpx
import pytest
import respx

from jobpulse_core.domain.models import SourceCheckpoint, SourceDefinition, SourceKind
from jobpulse_core.errors import ConfigurationError, SourceParseError
from jobpulse_core.ingestion.http import HttpClientConfig, SafeHttpClient
from jobpulse_core.ingestion.normalize import normalize_job
from jobpulse_core.sources import build_source
from tests.conftest import fixture_bytes

HTTP = HttpClientConfig(skip_dns_check=True, respect_robots_txt=False)


def definition(kind: SourceKind, **extra: object) -> SourceDefinition:
    return SourceDefinition(kind=kind, company_name="Acme", company_domain="acme.io", **extra)  # type: ignore[arg-type]


@respx.mock
async def test_greenhouse_contract() -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, content=fixture_bytes("greenhouse/board.json"), headers={"etag": '"v1"'}),
    )
    async with SafeHttpClient(HTTP) as http:
        result = await build_source(definition(SourceKind.GREENHOUSE, board_token="acme"), http).discover(
            SourceCheckpoint()
        )

    assert [job.external_id for job in result.jobs] == ["4012345", "4012346"]
    assert len(result.skipped) == 1  # empty title rejected, not silently accepted
    assert result.checkpoint.etag == '"v1"'
    first = normalize_job(result.jobs[0])
    assert first.canonical_url == "https://boards.greenhouse.io/acme/jobs/4012345"
    assert "<script>" not in first.description_html
    assert "4+ years" in first.description_text
    assert first.department == "Engineering"


@respx.mock
async def test_lever_contract() -> None:
    respx.get("https://api.lever.co/v0/postings/globex?mode=json").mock(
        return_value=httpx.Response(200, content=fixture_bytes("lever/postings.json")),
    )
    async with SafeHttpClient(HTTP) as http:
        result = await build_source(definition(SourceKind.LEVER, board_token="globex"), http).discover(
            SourceCheckpoint()
        )

    assert len(result.jobs) == 2
    ml = result.jobs[0]
    assert ml.remote_hint is True
    assert ml.location == "Bangalore, India"
    assert "3-5 years" in (ml.description_html or "")
    assert result.jobs[1].remote_hint is False
    assert ml.published_at is not None


@respx.mock
async def test_ashby_contract() -> None:
    respx.get("https://api.ashbyhq.com/posting-api/job-board/initech?includeCompensation=false").mock(
        return_value=httpx.Response(200, content=fixture_bytes("ashby/board.json")),
    )
    async with SafeHttpClient(HTTP) as http:
        result = await build_source(definition(SourceKind.ASHBY, board_token="initech"), http).discover(
            SourceCheckpoint()
        )

    assert len(result.jobs) == 1  # unlisted job excluded
    job = result.jobs[0]
    assert job.location == "Remote, India, Europe"
    assert job.remote_hint is True
    assert job.description_text is not None


@respx.mock
async def test_payload_drift_fails_loudly() -> None:
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true").mock(
        return_value=httpx.Response(200, json={"postings": []}),
    )
    async with SafeHttpClient(HTTP) as http:
        with pytest.raises(SourceParseError):
            await build_source(definition(SourceKind.GREENHOUSE, board_token="acme"), http).discover(SourceCheckpoint())


@respx.mock
async def test_not_modified_short_circuits() -> None:
    respx.get("https://api.lever.co/v0/postings/globex?mode=json").mock(return_value=httpx.Response(304))
    async with SafeHttpClient(HTTP) as http:
        checkpoint = SourceCheckpoint(etag='"x"')
        result = await build_source(definition(SourceKind.LEVER, board_token="globex"), http).discover(checkpoint)
    assert result.not_modified
    assert result.jobs == []
    assert result.checkpoint == checkpoint


@respx.mock
async def test_rss_feed() -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Jobs</title>
    <item><guid>rss-1</guid><title>Remote Python Developer</title><link>https://jobs.example.com/1?utm_source=rss</link>
    <description>&lt;p&gt;Remote, worldwide. 3+ years of experience.&lt;/p&gt;</description>
    <pubDate>Mon, 05 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
    respx.get("https://jobs.example.com/feed.xml").mock(return_value=httpx.Response(200, content=feed))
    async with SafeHttpClient(HTTP) as http:
        source = build_source(definition(SourceKind.RSS, url="https://jobs.example.com/feed.xml"), http)
        result = await source.discover(SourceCheckpoint())
    assert len(result.jobs) == 1
    assert result.jobs[0].external_id == "rss-1"
    assert normalize_job(result.jobs[0]).canonical_url == "https://jobs.example.com/1"


@respx.mock
async def test_generic_json_with_field_map() -> None:
    respx.get("https://api.example.com/jobs").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "items": [{"uid": 7, "name": "Data Engineer", "link": "/jobs/7", "where": "India", "remote": True}]
                }
            },
        ),
    )
    field_map = {
        "items": "data.items",
        "external_id": "uid",
        "title": "name",
        "url": "link",
        "location": "where",
        "remote": "remote",
    }
    async with SafeHttpClient(HTTP) as http:
        source = build_source(
            definition(SourceKind.GENERIC_JSON, url="https://api.example.com/jobs", field_map=field_map), http
        )
        result = await source.discover(SourceCheckpoint())
    job = result.jobs[0]
    assert job.url == "https://api.example.com/jobs/7"
    assert job.remote_hint is True


async def test_generic_json_requires_field_map() -> None:
    async with SafeHttpClient(HTTP) as http:
        with pytest.raises(ConfigurationError):
            build_source(definition(SourceKind.GENERIC_JSON, url="https://api.example.com/jobs"), http)


@respx.mock
async def test_static_html_listing() -> None:
    html = b"""<html><body><ul>
      <li class="job"><a class="t" href="/careers/ai-eng">AI Engineer</a><span class="loc">Remote</span></li>
      <li class="job"><a class="t" href="https://acme.io/careers/sre">SRE</a><span class="loc">Pune, India</span></li>
      <li class="job"><span>broken</span></li></ul></body></html>"""
    respx.get("https://acme.io/careers").mock(return_value=httpx.Response(200, content=html))
    field_map = {"item": "li.job", "title": "a.t", "location": ".loc"}
    async with SafeHttpClient(HTTP) as http:
        source = build_source(
            definition(SourceKind.STATIC_HTML, url="https://acme.io/careers", field_map=field_map), http
        )
        result = await source.discover(SourceCheckpoint())
    assert [job.title for job in result.jobs] == ["AI Engineer", "SRE"]
    assert result.jobs[0].url == "https://acme.io/careers/ai-eng"
    assert len(result.skipped) == 1


def test_definition_requires_locator() -> None:
    with pytest.raises(ValueError, match="board_token"):
        SourceDefinition(kind=SourceKind.GREENHOUSE, company_name="Acme", company_domain="acme.io")
    with pytest.raises(ValueError, match="url"):
        SourceDefinition(kind=SourceKind.RSS, company_name="Acme", company_domain="acme.io")
