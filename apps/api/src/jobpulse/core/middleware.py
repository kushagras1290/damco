"""Pure-ASGI middleware: request IDs + access logging + metrics, security headers,
request-size limits and per-client rate limiting.

Implemented as raw ASGI (not BaseHTTPMiddleware) so request bodies are streamed and
limits are enforced before the payload is buffered.
"""

from __future__ import annotations

import ipaddress
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobpulse.core.errors import problem_response
from jobpulse.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = structlog.get_logger("jobpulse.access")

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
RATE_LIMIT_EXEMPT_PREFIXES = ("/health", "/metrics")
MAX_TRACKED_CLIENTS = 10_000

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


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated: float


class RateLimitMiddleware:
    """In-process token bucket per client IP.

    Adequate for a single API instance (v1). Horizontal scaling would move this to the
    edge (Render/Cloudflare) - documented in docs/tradeoffs.md.
    """

    def __init__(self, app: ASGIApp, *, per_minute: int, trusted_proxy_count: int) -> None:
        self.app = app
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60.0
        self.trusted_proxy_count = trusted_proxy_count
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

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

    def _allow(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, updated=now)
            self._buckets[key] = bucket
            if len(self._buckets) > MAX_TRACKED_CLIENTS:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)
        bucket.tokens = min(self.capacity, bucket.tokens + (now - bucket.updated) * self.refill_per_second)
        bucket.updated = now
        if bucket.tokens >= 1:
            bucket.tokens -= 1
            return True, 0.0
        return False, (1 - bucket.tokens) / self.refill_per_second

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(RATE_LIMIT_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        allowed, retry_after = self._allow(self._client_ip(scope))
        if not allowed:
            response = problem_response(
                429,
                "rate_limited",
                "too many requests",
                Request(scope),
                headers={"Retry-After": str(max(1, int(retry_after) + 1))},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
