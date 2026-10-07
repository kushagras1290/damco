"""Pure-ASGI middleware: request IDs + access logging + metrics, security headers,
request-size limits and per-client rate limiting.

Implemented as raw ASGI (not BaseHTTPMiddleware) so request bodies are streamed and
limits are enforced before the payload is buffered.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobpulse.core.errors import problem_response
from jobpulse.core.metrics import HTTP_LATENCY, HTTP_REQUESTS
from jobpulse.core.ratelimit import RateDecision, RateLimiter

logger = structlog.get_logger("jobpulse.access")

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
RATE_LIMIT_EXEMPT_PREFIXES = ("/health", "/metrics")

SECURITY_HEADERS: dict[str, str] = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-site",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
    "cache-control": "no-store",
}
HSTS_VALUE = "max-age=63072000; includeSubDomains"


def _route_template(scope: Scope) -> str:
    route = scope.get("route")
    return getattr(route, "path", "unmatched")


class RequestContextMiddleware:
    """Request ID propagation, structured access log, Prometheus metrics, security headers."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if REQUEST_ID_RE.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
                if self.hsts:
                    headers.setdefault("strict-transport-security", HSTS_VALUE)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(scope)
            method = scope["method"]
            status = status_holder["status"]
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
            HTTP_LATENCY.labels(method=method, route=route).observe(elapsed)
            if not route.startswith(RATE_LIMIT_EXEMPT_PREFIXES):
                logger.info(
                    "http.request",
                    method=method,
                    route=route,
                    status=status,
                    duration_ms=round(elapsed * 1000, 2),
                )
            structlog.contextvars.unbind_contextvars("request_id")


class BodySizeLimitMiddleware:
    """Reject bodies larger than ``max_bytes`` (declared or streamed) with 413."""

    def __init__(self, app: ASGIApp, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await problem_response(413, "payload_too_large", "request body too large", Request(scope))(
                scope, receive, send
            )
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLargeError:
            await problem_response(413, "payload_too_large", "request body too large", Request(scope))(
                scope, receive, send
            )


class _BodyTooLargeError(Exception):
    """Internal control-flow signal for streamed oversize bodies."""


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    window_seconds: int
    anonymous: int
    authenticated: int
    writes: int
    stream_connects: int


@dataclass(frozen=True, slots=True)
class CallerIdentity:
    key: str
    authenticated: bool


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STREAM_PREFIX = "/api/v1/events"


class RateLimitMiddleware:
    """Tiered sliding-window limits backed by a shared limiter (Redis, with local fallback).

    Buckets, all per caller (verified owner > signed visitor id > client IP):
      * requests: ``anonymous`` or ``authenticated`` limit per window
      * writes:   additional ``writes`` limit for state-changing methods
      * streams:  ``stream_connects`` limit for opening realtime streams
    Every response carries RateLimit-Limit / -Remaining / -Reset; 429s add Retry-After.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        policy: RateLimitPolicy,
        trusted_proxy_count: int,
        identify: Callable[[str], CallerIdentity | None] | None = None,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.policy = policy
        self.trusted_proxy_count = trusted_proxy_count
        self.identify = identify

    def _client_ip(self, scope: Scope) -> str:
        if self.trusted_proxy_count:
            forwarded = Headers(scope=scope).get("x-forwarded-for")
            if forwarded:
                hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
                if len(hops) >= self.trusted_proxy_count:
                    candidate = hops[-self.trusted_proxy_count]
                    try:
                        return str(ipaddress.ip_address(candidate))
                    except ValueError:
                        pass
        client = scope.get("client")
        return client[0] if client else "unknown"

    def _caller(self, scope: Scope) -> CallerIdentity:
        if self.identify is not None:
            header = Headers(scope=scope).get("authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() == "bearer" and token:
                identity = self.identify(token.strip())
                if identity is not None:
                    return identity
        return CallerIdentity(key=f"ip:{self._client_ip(scope)}", authenticated=False)

    async def _decide(self, scope: Scope) -> RateDecision:
        caller = self._caller(scope)
        policy = self.policy
        window = policy.window_seconds
        if scope["path"].startswith(STREAM_PREFIX):
            return await self.limiter.hit(f"stream:{caller.key}", limit=policy.stream_connects, window_seconds=window)
        limit = policy.authenticated if caller.authenticated else policy.anonymous
        decision = await self.limiter.hit(f"req:{caller.key}", limit=limit, window_seconds=window)
        if decision.allowed and scope["method"] in WRITE_METHODS:
            write = await self.limiter.hit(f"write:{caller.key}", limit=policy.writes, window_seconds=window)
            if not write.allowed or write.remaining < decision.remaining:
                return write
        return decision

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(RATE_LIMIT_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        decision = await self._decide(scope)
        rate_headers = {
            "RateLimit-Limit": str(decision.limit),
            "RateLimit-Remaining": str(decision.remaining),
            "RateLimit-Reset": str(decision.reset_seconds),
        }
        if not decision.allowed:
            response = problem_response(
                429,
                "rate_limited",
                "too many requests",
                Request(scope),
                headers={**rate_headers, "Retry-After": str(decision.reset_seconds)},
            )
            await response(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in rate_headers.items():
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestTimeoutMiddleware:
    """Server-side deadline per request (long-lived streams excluded).

    Prevents slow handlers from holding workers indefinitely; returns 504 if the deadline
    passes before the response has started.
    """

    def __init__(self, app: ASGIApp, *, timeout_seconds: float, exempt_prefixes: tuple[str, ...]) -> None:
        self.app = app
        self.timeout_seconds = timeout_seconds
        self.exempt_prefixes = exempt_prefixes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(self.exempt_prefixes):
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(self.timeout_seconds):
                await self.app(scope, receive, tracking_send)
        except TimeoutError:
            logger.warning("http.request_timeout", path=scope["path"], timeout_s=self.timeout_seconds)
            if not started:
                response = problem_response(504, "request_timeout", "request took too long", Request(scope))
                await response(scope, receive, send)
