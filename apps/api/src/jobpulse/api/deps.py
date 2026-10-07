"""FastAPI dependencies: app context, Temporal client, pagination (sessions: api.tenancy)."""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Query, Request
from temporalio.client import Client

from jobpulse.core.errors import ServiceUnavailableError
from jobpulse.services.context import AppContext
from jobpulse.services.temporal import WorkflowServiceError, connect

MAX_PAGE_SIZE = 100


def get_ctx(request: Request) -> AppContext:
    ctx: AppContext = request.app.state.ctx
    return ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


async def get_temporal(request: Request, ctx: Ctx) -> Client:
    state = request.app.state
    lock: asyncio.Lock = state.temporal_lock
    async with lock:
        client: Client | None = getattr(state, "temporal", None)
        if client is None:
            try:
                client = await connect(ctx.settings)
            except WorkflowServiceError as exc:
                raise ServiceUnavailableError("workflow engine unavailable") from exc
            state.temporal = client
    return client


Temporal = Annotated[Client, Depends(get_temporal)]


class Pagination:
    def __init__(
        self,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 25,
        offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
    ) -> None:
        self.limit = limit
        self.offset = offset


Paging = Annotated[Pagination, Depends()]
