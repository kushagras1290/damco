"""SSRF protection, allowlisting, redirects, size limits, robots.txt and status mapping."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from jobpulse_core.errors import (
    RobotsDisallowedError,
    SourceFetchError,
    SourceNotFoundError,
    SourceRateLimitedError,
    SourceResponseTooLargeError,
    UnsafeUrlError,
)
from jobpulse_core.ingestion.http import HttpClientConfig, SafeHttpClient, host_matches_allowlist


def client(**overrides: object) -> SafeHttpClient:
    config = HttpClientConfig(skip_dns_check=True, **overrides)  # type: ignore[arg-type]
    return SafeHttpClient(config)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "http://100.64.0.1/",
        "http://localhost/",
        "http://metadata.google.internal/",
        "https://example.com:8080/",
        "https://user:pass@example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
    ],
)
async def test_rejects_unsafe_targets(url: str) -> None:
    async with client() as http:
        with pytest.raises(UnsafeUrlError):
            await http.validate_url(url)


async def test_dns_resolution_to_private_ip_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(*_: object, **__: object) -> list[tuple[object, ...]]:
        return [(2, 1, 6, "", ("10.1.2.3", 443))]

    async with SafeHttpClient(HttpClientConfig()) as http:
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(UnsafeUrlError, match="non-public"):
            await http.validate_url("https://rebind.example.com/")


def test_allowlist_matching() -> None:
    assert host_matches_allowlist("api.lever.co", ["api.lever.co"])
    assert host_matches_allowlist("jobs.acme.io", ["acme.io"])
    assert not host_matches_allowlist("evilacme.io", ["acme.io"])


async def test_allowlist_enforced() -> None:
    async with client(allowed_host_suffixes=("api.lever.co",)) as http:
        with pytest.raises(UnsafeUrlError, match="allowlist"):
            await http.validate_url("https://evil.example.com/")


@respx.mock
async def test_redirect_to_private_address_is_blocked() -> None:
    respx.get("https://jobs.example.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://jobs.example.com/feed").mock(
        return_value=httpx.Response(302, headers={"location": "http://169.254.169.254/latest"}),
    )
    async with client() as http:
        with pytest.raises(UnsafeUrlError):
            await http.fetch("https://jobs.example.com/feed")


@respx.mock
async def test_response_size_limit() -> None:
    respx.get("https://jobs.example.com/big").mock(return_value=httpx.Response(200, content=b"x" * 2048))
    async with client(max_response_bytes=1024, respect_robots_txt=False) as http:
        with pytest.raises(SourceResponseTooLargeError):
            await http.fetch("https://jobs.example.com/big")


@respx.mock
async def test_robots_disallow_is_respected() -> None:
    respx.get("https://jobs.example.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private"),
    )
    async with client() as http:
        with pytest.raises(RobotsDisallowedError):
            await http.fetch("https://jobs.example.com/private/jobs")


@respx.mock
@pytest.mark.parametrize(
    ("status", "headers", "error"),
    [
        (429, {"retry-after": "120"}, SourceRateLimitedError),
        (503, {}, SourceFetchError),
        (404, {}, SourceNotFoundError),
        (401, {}, SourceNotFoundError),
    ],
)
async def test_status_classification(status: int, headers: dict[str, str], error: type[Exception]) -> None:
    respx.get("https://jobs.example.com/x").mock(return_value=httpx.Response(status, headers=headers))
    async with client(respect_robots_txt=False) as http:
        with pytest.raises(error) as excinfo:
            await http.fetch("https://jobs.example.com/x")
    if isinstance(excinfo.value, SourceRateLimitedError):
        assert excinfo.value.retry_after_seconds == 120
        assert excinfo.value.retryable


@respx.mock
async def test_conditional_get_not_modified() -> None:
    route = respx.get("https://jobs.example.com/feed").mock(return_value=httpx.Response(304))
    async with client(respect_robots_txt=False) as http:
        result = await http.fetch("https://jobs.example.com/feed", etag='"abc"')
    assert result.not_modified
    assert route.calls.last.request.headers["if-none-match"] == '"abc"'


@respx.mock
async def test_timeout_is_retryable_fetch_error() -> None:
    respx.get("https://jobs.example.com/slow").mock(side_effect=httpx.ConnectTimeout("timeout"))
    async with client(respect_robots_txt=False) as http:
        with pytest.raises(SourceFetchError) as excinfo:
            await http.fetch("https://jobs.example.com/slow")
    assert excinfo.value.retryable
