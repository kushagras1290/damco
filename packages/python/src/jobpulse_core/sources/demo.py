"""Demo board: bundled sample postings released a few at a time (DEMO_MODE only).

Lets evaluators watch the realtime pipeline end to end without depending on what real
job boards happen to contain. Companies are fictional and use reserved ``.example``
domains.

The board never runs dry: every poll releases ``RELEASE_PER_POLL`` new postings and the
(full) listing shows only the most recent ``WINDOW`` of them, so the oldest close - like a
real board's churn. Once the bundled templates are used up they are re-posted with a
cycle suffix (``<id>-r<n>``), which are new jobs. Steady arrivals also keep adaptive
polling at its fastest interval, so the demo stays live however long it runs.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from jobpulse_core.domain.models import RawJob, SourceCheckpoint, SourceDefinition
from jobpulse_core.ingestion.http import FetchResult
from jobpulse_core.sources.base import DiscoveryResult

DEMO_BOARD_FILE = Path(__file__).with_name("demo_board.json")
INITIAL_RELEASE = 6
RELEASE_PER_POLL = 3
CURSOR_KEY = "demo_released"
WINDOW = 30  # postings visible on the board at once
MINUTES_BETWEEN_POSTINGS = 7  # spreads publish times so freshness scoring varies


@lru_cache(maxsize=1)
def demo_postings() -> tuple[dict[str, Any], ...]:
    raw = DEMO_BOARD_FILE.read_text(encoding="utf-8")
    return tuple(json.loads(raw)["jobs"])


class DemoSource:
    def __init__(self, definition: SourceDefinition) -> None:
        self.definition = definition

    async def discover(self, checkpoint: SourceCheckpoint) -> DiscoveryResult:
        postings = demo_postings()
        released_before = int(checkpoint.cursor.get(CURSOR_KEY, 0))
        released = max(INITIAL_RELEASE, released_before + RELEASE_PER_POLL)
        now = datetime.now(tz=UTC)
        visible = range(max(0, released - WINDOW), released)
        jobs = [self._to_raw(postings[n % len(postings)], now, n, released) for n in visible]
        payload = json.dumps({"jobs": [job.raw for job in jobs]}).encode("utf-8")
        result = FetchResult(
            url="demo://board", status_code=200, headers={"content-type": "application/json"}, content=payload
        )
        next_checkpoint = SourceCheckpoint(
            cursor={**checkpoint.cursor, CURSOR_KEY: released},
            last_seen_external_ids=[job.external_id for job in jobs],
        )
        return DiscoveryResult(
            jobs=jobs, checkpoint=next_checkpoint, raw_payload=result.content, content_type="application/json"
        )

    @staticmethod
    def _to_raw(posting: dict[str, Any], now: datetime, sequence: int, released: int) -> RawJob:
        # Newest releases look newest: publish time counts back from the latest posting.
        published = now - timedelta(minutes=MINUTES_BETWEEN_POSTINGS * (released - 1 - sequence))
        cycle = sequence // len(demo_postings())
        external_id = posting["id"] if cycle == 0 else f"{posting['id']}-r{cycle}"
        return RawJob(
            external_id=external_id,
            title=posting["title"],
            url=f"https://jobs.{posting['domain']}/postings/{external_id}",
            company_name=posting["company"],
            company_domain=posting["domain"],
            location=posting.get("location"),
            department=posting.get("department"),
            description_html=posting["html"],
            published_at=published,
            remote_hint=posting.get("remote"),
            raw=posting,
        )
