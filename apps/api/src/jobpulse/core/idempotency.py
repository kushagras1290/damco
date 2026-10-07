"""Idempotency-Key support for state-changing requests (Stripe-style semantics).

A client that retries a POST/PATCH with the same ``Idempotency-Key`` gets the original
response replayed (``Idempotent-Replayed: true``) instead of executing twice:

* same key + same request   -> stored response is replayed (for 24 h by default)
* same key + different body -> 422 (the key was reused for another request)
* same key still executing  -> 409 (retry shortly)
* Redis down / circuit open -> 503 for keyed requests (never a silent duplicate);
                                requests without the header are unaffected

Keys are scoped per caller (verified identity) and per method + path.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypedDict

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from jobpulse.core.circuit import CircuitBreaker
from jobpulse.core.errors import problem_response

logger = structlog.get_logger(__name__)

HEADER = "idempotency-key"
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,255}$")
KEY_PREFIX = "jp:idem:"
LOCK_TTL_SECONDS = 60
MUTATING_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})
MAX_STORED_BODY_BYTES = 512 * 1024
NOT_REPLAYED_HEADERS = frozenset({"content-length", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset"})


class StoredResponse(TypedDict):
    fingerprint: str
    status: int
    headers: list[tuple[str, str]]
    body: str


@dataclass(slots=True)
class _Captured:
    status: int = 500
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytearray = field(default_factory=bytearray)


class IdempotencyMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        redis: Redis | None,
        ttl_seconds: int,
        caller_key: Callable[[Scope], str],
        breaker: CircuitBreaker,
    ) -> None:
        self.app = app
        self.redis = redis
        self.breaker = breaker
        self.ttl_seconds = ttl_seconds
        self.caller_key = caller_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        raw_key = Headers(scope=scope).get(HEADER)
        if raw_key is None:
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        if not KEY_RE.fullmatch(raw_key):
            response = problem_response(
                422, "invalid_idempotency_key", "Idempotency-Key must be 8-255 characters of [A-Za-z0-9_-]", request
            )
            await response(scope, receive, send)
            return
        if self.redis is None or not self.breaker.allow():
            await _unavailable(request)(scope, receive, send)
            return

        body = await _read_body(receive)
        fingerprint = hashlib.sha256(f"{scope['method']} {scope['path']}\0".encode() + body).hexdigest()
        scope_material = f"{self.caller_key(scope)}|{scope['method']}|{scope['path']}|{raw_key}"
        key = f"{KEY_PREFIX}{hashlib.sha256(scope_material.encode()).hexdigest()}"
        lock_key = f"{key}:lock"

        try:
            stored = await self.redis.get(key)
            acquired = stored is None and bool(
                await self.redis.set(lock_key, fingerprint, nx=True, ex=LOCK_TTL_SECONDS)
            )
        except (RedisError, OSError) as exc:
            self.breaker.record_failure()
            logger.warning("idempotency.unavailable", error=type(exc).__name__)
            await _unavailable(request)(scope, receive, send)
            return
        self.breaker.record_success()

        if stored is not None:
            record: StoredResponse = json.loads(stored)
            await _replay(record, fingerprint, request, scope, send)
            return
        if not acquired:
            response = problem_response(
                409,
                "idempotency_in_progress",
                "a request with this Idempotency-Key is still in progress",
                request,
                headers={"Retry-After": "1"},
            )
            await response(scope, receive, send)
            return

        captured = _Captured()

        async def capture_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                captured.status = message["status"]
                captured.headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in message.get("headers", [])]
            elif message["type"] == "http.response.body":
                captured.body.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, _replay_receive(body), capture_send)
        finally:
            await self._finish(self.redis, key, lock_key, fingerprint, captured)

    async def _finish(self, redis: Redis, key: str, lock_key: str, fingerprint: str, captured: _Captured) -> None:
        try:
            # Remember only definitive outcomes: 5xx and 429 may legitimately be retried.
            definitive = captured.status < 500 and captured.status != 429
            if definitive and len(captured.body) <= MAX_STORED_BODY_BYTES:
                record: StoredResponse = {
                    "fingerprint": fingerprint,
                    "status": captured.status,
                    "headers": captured.headers,
                    "body": captured.body.decode("utf-8", errors="replace"),
                }
                await redis.set(key, json.dumps(record), ex=self.ttl_seconds)
            await redis.delete(lock_key)
        except (RedisError, OSError) as exc:
            logger.warning("idempotency.store_failed", error=type(exc).__name__)


def _unavailable(request: Request) -> JSONResponse:
    return problem_response(
        503,
        "idempotency_unavailable",
        "idempotency store unavailable, retry shortly",
        request,
        headers={"Retry-After": "5"},
    )


async def _replay(record: StoredResponse, fingerprint: str, request: Request, scope: Scope, send: Send) -> None:
    if record["fingerprint"] != fingerprint:
        response = problem_response(
            422, "idempotency_key_reused", "Idempotency-Key was already used for a different request", request
        )
        await response(scope, _disconnected, send)
        return
    body = record["body"].encode("utf-8")
    headers = [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in record["headers"]
        if name.lower() not in NOT_REPLAYED_HEADERS
    ]
    headers += [(b"content-length", str(len(body)).encode()), (b"idempotent-replayed", b"true")]
    await send({"type": "http.response.start", "status": record["status"], "headers": headers})
    await send({"type": "http.response.body", "body": body})


async def _read_body(receive: Receive) -> bytes:
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _replay_receive(body: bytes) -> Receive:
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    return receive


async def _disconnected() -> Message:
    return {"type": "http.disconnect"}
