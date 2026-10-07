"""Outbound HTTP client with SSRF protection, host allowlisting, robots.txt compliance,
manual redirect validation, response-size limits and strict timeouts.

Every external fetch in JobPulse goes through :class:`SafeHttpClient`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import structlog

from jobpulse_core.errors import (
    RobotsDisallowedError,
    SourceFetchError,
    SourceNotFoundError,
    SourceRateLimitedError,
    SourceResponseTooLargeError,
    UnsafeUrlError,
)

logger = structlog.get_logger(__name__)

DEFAULT_USER_AGENT = "JobPulseBot/0.1 (+https://github.com/jobpulse; job discovery)"
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_REDIRECTS = 5
ROBOTS_CACHE_TTL_SECONDS = 3600.0
ROBOTS_MAX_BYTES = 512 * 1024
REDIRECT_STATUSES: frozenset[int] = frozenset({301, 302, 303, 307, 308})
ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})


@dataclass(frozen=True, slots=True)
class HttpClientConfig:
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 20.0
    total_timeout_seconds: float = 30.0
    max_response_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    user_agent: str = DEFAULT_USER_AGENT
    # Host suffixes that may be fetched. Empty means "any public host" (still SSRF-checked).
    allowed_host_suffixes: tuple[str, ...] = ()
    respect_robots_txt: bool = True
    # Test-only escape hatch: skips DNS resolution checks for mocked transports.
    skip_dns_check: bool = False


@dataclass(frozen=True, slots=True)
class FetchResult:
    url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes
    not_modified: bool = False
    elapsed_ms: float = 0.0

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "application/octet-stream").split(";")[0].strip()

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


@dataclass(slots=True)
class _RobotsEntry:
    parser: RobotFileParser | None
    fetched_at: float
    allow_all: bool = False


@dataclass(slots=True)
class _RobotsCache:
    entries: dict[str, _RobotsEntry] = field(default_factory=dict)
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)


def _is_public_ip(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10"))
    )


def host_matches_allowlist(host: str, suffixes: Iterable[str]) -> bool:
    host = host.lower().rstrip(".")
    for suffix in suffixes:
        normalized = suffix.lower().strip().lstrip(".")
        if normalized and (host == normalized or host.endswith(f".{normalized}")):
            return True
    return False


class SafeHttpClient:
    """Async HTTP client hardened for fetching untrusted external URLs."""

    def __init__(
        self,
        config: HttpClientConfig | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config or HttpClientConfig()
        timeout = httpx.Timeout(
            self._config.read_timeout_seconds,
            connect=self._config.connect_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            headers={"User-Agent": self._config.user_agent, "Accept-Encoding": "gzip, deflate"},
            transport=transport,
            trust_env=False,
        )
        self._robots = _RobotsCache()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def config(self) -> HttpClientConfig:
        return self._config

    # ----------------------------------------------------------------- validation

    async def validate_url(self, url: str) -> None:
        """Raise :class:`UnsafeUrlError` unless ``url`` targets an allowed public host."""
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise UnsafeUrlError("scheme not allowed", context={"url": url})
        if parts.username or parts.password:
            raise UnsafeUrlError("credentials in URL are not allowed", context={"url": url})
        host = (parts.hostname or "").lower()
        if not host:
            raise UnsafeUrlError("missing host", context={"url": url})
        try:
            port = parts.port
        except ValueError as exc:
            raise UnsafeUrlError("invalid port", context={"url": url}) from exc
        if port is not None and port not in ALLOWED_PORTS:
            raise UnsafeUrlError("port not allowed", context={"url": url, "port": port})
        if self._config.allowed_host_suffixes and not host_matches_allowlist(
            host,
            self._config.allowed_host_suffixes,
        ):
            raise UnsafeUrlError("host not in allowlist", context={"host": host})

        try:
            literal = ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            literal = None
        if literal is not None:
            if not _is_public_ip(str(literal)):
                raise UnsafeUrlError("non-public IP literal", context={"host": host})
            return
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise UnsafeUrlError("internal hostname", context={"host": host})
        if self._config.skip_dns_check:
            return
        await self._assert_resolves_public(host, port or (443 if parts.scheme == "https" else 80))

    async def _assert_resolves_public(self, host: str, port: int) -> None:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
                timeout=self._config.connect_timeout_seconds,
            )
        except (TimeoutError, socket.gaierror) as exc:
            raise SourceFetchError("DNS resolution failed", context={"host": host}) from exc
        addresses = {str(info[4][0]) for info in infos}
        if not addresses:
            raise SourceFetchError("DNS returned no addresses", context={"host": host})
        blocked = sorted(addr for addr in addresses if not _is_public_ip(addr))
        if blocked:
            raise UnsafeUrlError(
                "host resolves to non-public address",
                context={"host": host, "addresses": blocked},
            )

    # ----------------------------------------------------------------- robots.txt

    async def _robots_allows(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        lock = self._robots.locks.setdefault(origin, asyncio.Lock())
        async with lock:
            entry = self._robots.entries.get(origin)
            if entry is None or time.monotonic() - entry.fetched_at > ROBOTS_CACHE_TTL_SECONDS:
                entry = await self._fetch_robots(origin)
                self._robots.entries[origin] = entry
        if entry.allow_all or entry.parser is None:
            return True
        return entry.parser.can_fetch(self._config.user_agent, url)

    async def _fetch_robots(self, origin: str) -> _RobotsEntry:
        robots_url = f"{origin}/robots.txt"
        try:
            response = await self._send_once("GET", robots_url, headers={}, max_bytes=ROBOTS_MAX_BYTES)
        except SourceResponseTooLargeError:
            logger.warning("robots.oversized", origin=origin)
            return _RobotsEntry(parser=None, fetched_at=time.monotonic(), allow_all=True)
        status = response.status_code
        # RFC 9309: 4xx => no restrictions; 5xx => assume full disallow (retry later).
        if 400 <= status < 500:
            return _RobotsEntry(parser=None, fetched_at=time.monotonic(), allow_all=True)
        if status >= 500:
            raise SourceFetchError("robots.txt unavailable", context={"origin": origin, "status": status})
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return _RobotsEntry(parser=parser, fetched_at=time.monotonic())

    # ----------------------------------------------------------------- fetching

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        max_bytes: int,
        json_body: Any = None,
        content: bytes | None = None,
    ) -> FetchResult:
        await self.validate_url(url)
        started = time.perf_counter()
        try:
            request = self._client.build_request(
                method,
                url,
                headers=dict(headers),
                json=json_body if content is None else None,
                content=content,
            )
            async with asyncio.timeout(self._config.total_timeout_seconds):
                response = await self._client.send(request, stream=True)
                try:
                    declared = response.headers.get("content-length")
                    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
                        raise SourceResponseTooLargeError(
                            "declared content-length exceeds limit",
                            context={"url": url, "bytes": int(declared)},
                        )
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes():
                        buffer.extend(chunk)
                        if len(buffer) > max_bytes:
                            raise SourceResponseTooLargeError(
                                "response exceeds limit",
                                context={"url": url, "limit": max_bytes},
                            )
                finally:
                    await response.aclose()
        except TimeoutError as exc:
            raise SourceFetchError("request timed out", context={"url": url}) from exc
        except httpx.TransportError as exc:
            raise SourceFetchError(f"transport error: {type(exc).__name__}", context={"url": url}) from exc
        return FetchResult(
            url=url,
            status_code=response.status_code,
            headers={k.lower(): v for k, v in response.headers.items()},
            content=bytes(buffer),
            elapsed_ms=(time.perf_counter() - started) * 1000,
        )

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchResult:
        """Fetch ``url`` following validated redirects. Raises typed errors for non-2xx."""
        request_headers: dict[str, str] = dict(headers or {})
        if etag:
            request_headers["If-None-Match"] = etag
        if last_modified:
            request_headers["If-Modified-Since"] = last_modified

        current = url
        for _hop in range(self._config.max_redirects + 1):
            if self._config.respect_robots_txt and not await self._robots_allows(current):
                raise RobotsDisallowedError("robots.txt disallows URL", context={"url": current})
            result = await self._send_once(
                method,
                current,
                headers=request_headers,
                max_bytes=self._config.max_response_bytes,
                json_body=json_body,
                content=content,
            )
            status = result.status_code
            if status in REDIRECT_STATUSES:
                location = result.headers.get("location")
                if not location:
                    raise SourceFetchError("redirect without location", context={"url": current})
                current = urljoin(current, location)
                if status == 303:
                    method, json_body, content = "GET", None, None
                continue
            return self._classify(result)
        raise SourceFetchError("too many redirects", context={"url": url})

    @staticmethod
    def _classify(result: FetchResult) -> FetchResult:
        status = result.status_code
        if status == 304:
            return FetchResult(
                url=result.url,
                status_code=status,
                headers=result.headers,
                content=b"",
                not_modified=True,
                elapsed_ms=result.elapsed_ms,
            )
        if 200 <= status < 300:
            return result
        context: dict[str, object] = {"url": result.url, "status": status}
        if status == 429:
            retry_after = result.headers.get("retry-after")
            seconds = float(retry_after) if retry_after and retry_after.isdigit() else None
            raise SourceRateLimitedError("rate limited", retry_after_seconds=seconds, context=context)
        if status in {404, 410}:
            raise SourceNotFoundError("resource not found", context=context)
        if status >= 500 or status in {408, 425}:
            raise SourceFetchError("transient upstream error", context=context)
        # Remaining 4xx: permanent misconfiguration (auth, bad board token...).
        raise SourceNotFoundError("client error from source", context=context)
