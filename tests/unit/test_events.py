from __future__ import annotations

import asyncio
import json

import pytest

from jobpulse.api.routes.events import _stream
from jobpulse.services.event_hub import EventHub, HubFullError
from jobpulse.services.events import MAX_PAYLOAD_BYTES, Audience, EventRoute, EventType, encode_event, route_of


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


WS_A, WS_B = "ws-a", "ws-b"
A = Audience(workspace_id=WS_A, source_ids=frozenset({"board-1"}))
B = Audience(workspace_id=WS_B, source_ids=frozenset({"board-2"}))


def event(**data: str) -> str:
    return json.dumps({"type": "job.matched", "ts": "now", "data": data})


@pytest.mark.parametrize(
    ("payload", "route"),
    [
        (event(workspace_id=WS_A, job_id="j"), EventRoute(workspace_id=WS_A)),
        (event(source_id="board-1"), EventRoute(source_id="board-1")),
        (event(workspace_id=WS_A, source_id="board-2"), EventRoute(workspace_id=WS_A)),  # tenant wins
        (event(job_id="j"), None),  # unroutable
        ("not json", None),
        ('{"type":"x"}', None),
    ],
)
def test_route_of(payload: str, route: EventRoute | None) -> None:
    assert route_of(payload) == route


def test_audience_only_accepts_its_workspace_and_followed_boards() -> None:
    assert A.accepts(EventRoute(workspace_id=WS_A))
    assert not A.accepts(EventRoute(workspace_id=WS_B))
    assert A.accepts(EventRoute(source_id="board-1"))
    assert not A.accepts(EventRoute(source_id="board-2"))
    assert not Audience(workspace_id=None).accepts(EventRoute(workspace_id=WS_A))


async def test_hub_delivers_each_event_only_to_its_audience() -> None:
    hub = EventHub(None, max_clients=5, queue_size=10)
    async with hub.subscribe(A) as alice, hub.subscribe(B) as bob:
        hub.broadcast(event(workspace_id=WS_A, job_id="mine"))
        hub.broadcast(event(source_id="board-2"))
        hub.broadcast(event(job_id="nobody"))  # unroutable: dropped for everyone
        assert json.loads(alice.queue.get_nowait())["data"]["job_id"] == "mine"
        assert alice.queue.empty()
        assert json.loads(bob.queue.get_nowait())["data"]["source_id"] == "board-2"
        assert bob.queue.empty()
    assert hub.subscriber_count == 0


async def test_followed_boards_can_change_while_connected() -> None:
    hub = EventHub(None, max_clients=5, queue_size=10)
    audience = Audience(workspace_id=WS_A)
    async with hub.subscribe(audience) as stream:
        hub.broadcast(event(source_id="board-9"))
        assert stream.queue.empty()
        audience.source_ids = frozenset({"board-9"})  # followed after connecting
        hub.broadcast(event(source_id="board-9"))
        assert not stream.queue.empty()


async def test_slow_consumer_is_dropped_not_buffered_forever() -> None:
    hub = EventHub(None, max_clients=5, queue_size=2)
    async with hub.subscribe(A) as slow:
        for _ in range(3):
            hub.broadcast(event(workspace_id=WS_A))
        assert slow.overflowed
        assert slow.closed.is_set()
        assert hub.subscriber_count == 0


async def test_subscriber_cap() -> None:
    hub = EventHub(None, max_clients=1, queue_size=2)
    async with hub.subscribe(A):
        with pytest.raises(HubFullError):
            async with hub.subscribe(B):
                pass


async def test_stream_emits_ready_heartbeat_and_events() -> None:
    hub = EventHub(None, max_clients=5, queue_size=10)
    async with hub.subscribe(A) as subscription:
        stream = _stream(subscription, heartbeat_seconds=0.05)
        assert (await anext(stream)).startswith("retry: ")
        assert "ready" in await anext(stream)
        assert await anext(stream) == ": keep-alive\n\n"  # idle -> heartbeat
        payload = event(workspace_id=WS_A)
        hub.broadcast(payload)
        assert await anext(stream) == f"id: 1\ndata: {payload}\n\n"
        subscription.closed.set()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(stream), timeout=1)
