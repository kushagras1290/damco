from __future__ import annotations

import asyncio
import json

import pytest

from jobpulse.api.routes.events import _stream
from jobpulse.services.event_hub import EventHub, HubFullError
from jobpulse.services.events import MAX_PAYLOAD_BYTES, EventType, encode_event


def test_encode_event_shape_and_clipping() -> None:
    body = json.loads(encode_event(EventType.JOB_MATCHED, {"title": "x" * 1000, "score": 0.9}))
    assert body["type"] == "job.matched"
    assert len(body["data"]["title"]) == 200
    assert body["ts"]


def test_encode_event_respects_notify_size_limit() -> None:
    jobs = [{"id": str(i), "title": "t" * 199, "company": "c" * 199} for i in range(500)]
    body = encode_event(EventType.JOBS_DISCOVERED, {"jobs": jobs, "new": 500})
    assert len(body.encode()) <= MAX_PAYLOAD_BYTES
    decoded = json.loads(body)
    assert decoded["data"]["truncated"] is True
    assert decoded["data"]["new"] == 500


async def test_hub_fans_out_to_all_subscribers() -> None:
    hub = EventHub(None, max_clients=5, queue_size=10)
    async with hub.subscribe() as first, hub.subscribe() as second:
        hub.broadcast('{"type":"x"}')
        assert first.queue.get_nowait() == '{"type":"x"}'
        assert second.queue.get_nowait() == '{"type":"x"}'
    assert hub.subscriber_count == 0


async def test_slow_consumer_is_dropped_not_buffered_forever() -> None:
    hub = EventHub(None, max_clients=5, queue_size=2)
    async with hub.subscribe() as slow:
        for _ in range(3):
            hub.broadcast("{}")
        assert slow.overflowed
        assert slow.closed.is_set()
        assert hub.subscriber_count == 0


async def test_subscriber_cap() -> None:
    hub = EventHub(None, max_clients=1, queue_size=2)
    async with hub.subscribe():
        with pytest.raises(HubFullError):
            async with hub.subscribe():
                pass


async def test_stream_emits_ready_heartbeat_and_events() -> None:
    hub = EventHub(None, max_clients=5, queue_size=10)
    async with hub.subscribe() as subscription:
        stream = _stream(subscription, heartbeat_seconds=0.05)
        assert (await anext(stream)).startswith("retry: ")
        assert "ready" in await anext(stream)
        assert await anext(stream) == ": keep-alive\n\n"  # idle -> heartbeat
        hub.broadcast('{"type":"job.matched"}')
        assert await anext(stream) == 'id: 1\ndata: {"type":"job.matched"}\n\n'
        subscription.closed.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
