"""Process-wide resource container shared by API handlers and worker activities."""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import TracebackType
from typing import Self

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from jobpulse.core.config import Settings
from jobpulse.db.session import create_engine, create_session_factory
from jobpulse.services.storage import SnapshotStore, build_snapshot_store
from jobpulse_core.ingestion.http import SafeHttpClient
from jobpulse_core.intelligence import IntelligenceService

logger = structlog.get_logger(__name__)

NOTIFY_MAX_RESPONSE_BYTES = 1024 * 1024


@dataclass(slots=True)
class AppContext:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    store: SnapshotStore
    intelligence: IntelligenceService | None
    notify_http: SafeHttpClient

    @classmethod
    def create(cls, settings: Settings) -> AppContext:
        engine = create_engine(settings)
        intelligence = (
            IntelligenceService(settings.intelligence_config()) if settings.intelligence_config().enabled else None
        )
        if intelligence is None:
            logger.warning("intelligence.disabled", reason="OPENAI_API_KEY not set; deterministic-only mode")
        # Notifications: webhook hosts are user-configured, so no allowlist, but the
        # SSRF guard (public IPs only, ports 80/443) still applies. robots.txt is
        # irrelevant for POSTing to our own endpoints.
        notify_http = SafeHttpClient(
            replace(
                settings.http_client_config(robots=False),
                max_response_bytes=NOTIFY_MAX_RESPONSE_BYTES,
                allowed_host_suffixes=(),
            ),
        )
        return cls(
            settings=settings,
            engine=engine,
            sessions=create_session_factory(engine),
            store=build_snapshot_store(settings),
            intelligence=intelligence,
            notify_http=notify_http,
        )

    def source_http(self, extra_hosts: list[str]) -> SafeHttpClient:
        """Fresh client per fetch so the allowlist includes this source's own host."""
        return SafeHttpClient(self.settings.http_client_config(extra_hosts=extra_hosts))

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
        await self.notify_http.aclose()
        if self.intelligence is not None:
            await self.intelligence.__aexit__(None, None, None)
        await self.engine.dispose()
