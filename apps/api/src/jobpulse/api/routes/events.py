"""/api/v1/events - realtime domain events as Server-Sent Events."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from jobpulse.api.deps import Ctx
from jobpulse.core.errors import ServiceUnavailableError
from jobpulse.core.security import Reader
from jobpulse.services.event_hub import EventHub, HubFullError, Subscription

router = APIRouter(prefix="/api/v1", tags=["events"])

RECONNECT_HINT_MS = 5000
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def get_hub(request: Request) -> EventHub:
    hub: EventHub = request.app.state.events
    return hub


async def _stream(subscription: Subscription, heartbeat_seconds: float) -> AsyncIterator[str]:
    yield f"retry: {RECONNECT_HINT_MS}\n\n"
    yield 'event: ready\ndata: {"type":"ready"}\n\n'
    sequence = itertools.count(1)  # numbers events only, not heartbeats
    while not subscription.closed.is_set():  # closed = dropped as a slow consumer; browser reconnects
        try:
            payload = await asyncio.wait_for(subscription.queue.get(), timeout=heartbeat_seconds)
        except TimeoutError:
            yield ": keep-alive\n\n"
            continue
        yield f"id: {next(sequence)}\ndata: {payload}\n\n"


@router.get("/events", include_in_schema=True)
async def events(request: Request, _: Reader, ctx: Ctx) -> StreamingResponse:
    hub = get_hub(request)
    if not hub.enabled:
        raise ServiceUnavailableError("realtime events are not enabled on this deployment")
    heartbeat = ctx.settings.sse_heartbeat_seconds

    async def body() -> AsyncIterator[str]:
        try:
            async with hub.subscribe() as subscription:
                async for chunk in _stream(subscription, heartbeat):
                    yield chunk
        except HubFullError:
            yield 'event: busy\ndata: {"type":"busy"}\n\n'

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)
