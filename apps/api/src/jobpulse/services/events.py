"""Realtime domain events over PostgreSQL LISTEN/NOTIFY.

Producers call :func:`publish` inside the same transaction as the data change, so an
event is delivered only if (and when) that change commits. Payloads are small JSON
documents (ids, titles, scores); clients re-fetch details through the normal API.

PostgreSQL limits NOTIFY payloads to 8000 bytes; we cap well below that.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

EVENT_CHANNEL = "jobpulse_events"
MAX_PAYLOAD_BYTES = 7000
MAX_TEXT_CHARS = 200
MAX_LIST_ITEMS = 20


class EventType(StrEnum):
    SOURCE_PROGRESS = "source.progress"  # sync stages: fetching / fetched / stored
    SOURCE_POLLED = "source.polled"
    JOBS_DISCOVERED = "jobs.discovered"
    JOB_EVALUATED = "job.evaluated"
    JOB_MATCHED = "job.matched"
    NOTIFICATION_SENT = "notification.sent"
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"


def _clip(value: Any) -> Any:
    if isinstance(value, str):
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, list):
        return [_clip(item) for item in value[:MAX_LIST_ITEMS]]
    if isinstance(value, dict):
        return {key: _clip(item) for key, item in value.items()}
    return value


def encode_event(event_type: EventType, data: dict[str, Any]) -> str:
    """Serialise an event, shrinking list fields until it fits the NOTIFY budget."""
    payload: dict[str, Any] = {
        "type": event_type.value,
        "ts": datetime.now(tz=UTC).isoformat(),
        "data": _clip(data),
    }
    body = json.dumps(payload, separators=(",", ":"), default=str)
    while len(body.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        lists = [key for key, value in payload["data"].items() if isinstance(value, list) and value]
        if not lists:
            payload["data"] = {"truncated": True}
            body = json.dumps(payload, separators=(",", ":"), default=str)
            break
        for key in lists:
            payload["data"][key] = payload["data"][key][: len(payload["data"][key]) // 2]
        payload["data"]["truncated"] = True
        body = json.dumps(payload, separators=(",", ":"), default=str)
    return body


async def publish(session: AsyncSession, event_type: EventType, data: dict[str, Any]) -> None:
    """Queue an event for delivery when the surrounding transaction commits."""
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": EVENT_CHANNEL, "payload": encode_event(event_type, data)},
    )
