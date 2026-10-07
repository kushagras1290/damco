"""In-process fan-out of PostgreSQL NOTIFY events to Server-Sent-Events subscribers.

One dedicated LISTEN connection per API process (outside the SQLAlchemy pool, autocommit)
feeds any number of SSE clients. Each client has a bounded queue: a client that cannot
keep up is disconnected (its browser reconnects and refetches) instead of letting
memory grow without bound.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

import psycopg
import structlog
from psycopg import sql

from jobpulse.services.events import EVENT_CHANNEL, Audience, route_of

# Composed once with identifier quoting (LISTEN cannot take a bind parameter).
LISTEN_STATEMENT = sql.SQL("LISTEN {}").format(sql.Identifier(EVENT_CHANNEL))

logger = structlog.get_logger(__name__)

RECONNECT_INITIAL_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
CONNECT_TIMEOUT_SECONDS = 10


class HubFullError(Exception):
    """Too many concurrent subscribers on this instance."""


@dataclass(eq=False)
class Subscription:
    queue: asyncio.Queue[str]
    audience: Audience
    overflowed: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class EventHub:
    def __init__(self, dsn: str | None, *, max_clients: int, queue_size: int) -> None:
        self._dsn = dsn
        self._max_clients = max_clients
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()
        self._task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return self._dsn is not None

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def wait_connected(self) -> None:
        """Block until the LISTEN connection is established (callers bound it with a timeout)."""
        await self._connected.wait()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def start(self) -> None:
        if self._dsn is not None and self._task is None:
            self._task = asyncio.create_task(self._listen_forever(self._dsn), name="event-hub-listener")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for subscription in list(self._subscribers):
            subscription.closed.set()
        self._subscribers.clear()

    @contextlib.asynccontextmanager
    async def subscribe(self, audience: Audience) -> AsyncGenerator[Subscription]:
        if len(self._subscribers) >= self._max_clients:
            raise HubFullError
        subscription = Subscription(queue=asyncio.Queue(maxsize=self._queue_size), audience=audience)
        self._subscribers.add(subscription)
        try:
            yield subscription
        finally:
            self._subscribers.discard(subscription)

    def broadcast(self, payload: str) -> None:
        """Deliver to every subscriber whose audience may see this event (parsed once)."""
        route = route_of(payload)
        if route is None:
            logger.warning("events.unroutable_dropped")
            return
        for subscription in list(self._subscribers):
            if not subscription.audience.accepts(route):
                continue
            try:
                subscription.queue.put_nowait(payload)
            except asyncio.QueueFull:
                subscription.overflowed = True
                subscription.closed.set()
                self._subscribers.discard(subscription)
                logger.warning("events.subscriber_dropped", reason="slow consumer")

    async def _listen_forever(self, dsn: str) -> None:
        delay = RECONNECT_INITIAL_SECONDS
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                    dsn, autocommit=True, connect_timeout=CONNECT_TIMEOUT_SECONDS
                ) as connection:
                    # Constant, identifier-quoted statement; no runtime input reaches it (Semgrep's
                    # sqlalchemy-execute-raw-query rule flags every non-literal execute argument).
                    await connection.execute(LISTEN_STATEMENT)  # nosemgrep
                    self._connected.set()
                    delay = RECONNECT_INITIAL_SECONDS
                    logger.info("events.listening", channel=EVENT_CHANNEL)
                    async for notification in connection.notifies():
                        self.broadcast(notification.payload)
            except asyncio.CancelledError:
                self._connected.clear()
                raise
            except (psycopg.Error, OSError) as exc:
                self._connected.clear()
                logger.warning("events.listener_disconnected", error=type(exc).__name__, retry_in=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, RECONNECT_MAX_SECONDS)
