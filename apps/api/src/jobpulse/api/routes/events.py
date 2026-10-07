"""/api/v1/events - realtime domain events as Server-Sent Events, filtered per tenant.

Each stream belongs to the caller's workspace: it receives that workspace's per-profile
events (evaluations, matches, alerts) and progress of the boards the workspace follows.
The followed-board set is refreshed while the stream is open.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from collections.abc import AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from jobpulse.api.deps import Ctx
from jobpulse.core.errors import ServiceUnavailableError
from jobpulse.core.security import Reader
from jobpulse.db.models import SourceSubscription
from jobpulse.db.session import transaction
from jobpulse.db.tenancy import SYSTEM_SCOPE, workspace_scope
from jobpulse.services.accounts import resolve_account
from jobpulse.services.context import AppContext
from jobpulse.services.event_hub import EventHub, HubFullError, Subscription
from jobpulse.services.events import Audience

router = APIRouter(prefix="/api/v1", tags=["events"])

RECONNECT_HINT_MS = 5000
AUDIENCE_REFRESH_SECONDS = 60
SSE_HEADERS = {"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"}


def get_hub(request: Request) -> EventHub:
    hub: EventHub = request.app.state.events
    return hub


async def _followed_sources(ctx: AppContext, workspace_id: str) -> frozenset[str]:
    async with transaction(ctx.sessions, scope=workspace_scope(workspace_id)) as session:
        rows = await session.execute(select(SourceSubscription.source_id))  # RLS: this workspace only
        return frozenset(str(row[0]) for row in rows.all())


async def _audience(ctx: AppContext, principal: Reader) -> Audience:
    async with transaction(ctx.sessions, scope=SYSTEM_SCOPE) as session:
        account = await resolve_account(session, principal, ctx.settings)
    if account.workspace is None:
        return Audience(workspace_id=None)  # no workspace: nothing to receive
    workspace_id = str(account.workspace.id)
    return Audience(workspace_id=workspace_id, source_ids=await _followed_sources(ctx, workspace_id))


async def _refresh(ctx: AppContext, audience: Audience) -> None:
    """Keep the followed-board set current while the stream is open (follow/unfollow)."""
    if audience.workspace_id is None:
        return
    while True:
        await asyncio.sleep(AUDIENCE_REFRESH_SECONDS)
        audience.source_ids = await _followed_sources(ctx, audience.workspace_id)


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
async def events(request: Request, principal: Reader, ctx: Ctx) -> StreamingResponse:
    hub = get_hub(request)
    if not hub.enabled:
        raise ServiceUnavailableError("realtime events are not enabled on this deployment")
    heartbeat = ctx.settings.sse_heartbeat_seconds
    audience = await _audience(ctx, principal)  # resolved before streaming: auth errors are plain HTTP

    async def body() -> AsyncIterator[str]:
        refresher = asyncio.create_task(_refresh(ctx, audience), name="sse-audience-refresh")
        try:
            async with hub.subscribe(audience) as subscription:
                async for chunk in _stream(subscription, heartbeat):
                    yield chunk
        except HubFullError:
            yield 'event: busy\ndata: {"type":"busy"}\n\n'
        finally:
            refresher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await refresher

    return StreamingResponse(body(), media_type="text/event-stream", headers=SSE_HEADERS)
