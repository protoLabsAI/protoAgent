"""Tests for the in-process event bus + /api/events SSE route (ADR 0003)."""

from __future__ import annotations

import asyncio

from events.bus import EventBus


def test_publish_fans_out_to_all_subscribers():
    async def run():
        bus = EventBus()
        a = bus.subscribe()
        b = bus.subscribe()
        # Prime both subscriptions (registers their queues).
        ta = asyncio.ensure_future(a.__anext__())
        tb = asyncio.ensure_future(b.__anext__())
        await asyncio.sleep(0)  # let the generators register
        bus.publish("hello", {"n": 1})
        ra, rb = await asyncio.wait_for(asyncio.gather(ta, tb), timeout=1)
        await a.aclose()
        await b.aclose()
        return ra, rb

    ra, rb = asyncio.run(run())
    assert ra == {"event": "hello", "data": {"n": 1}}
    assert rb == {"event": "hello", "data": {"n": 1}}


def test_publish_defaults_empty_data():
    async def run():
        bus = EventBus()
        sub = bus.subscribe()
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        bus.publish("ping")
        evt = await asyncio.wait_for(t, timeout=1)
        await sub.aclose()
        return evt

    assert asyncio.run(run()) == {"event": "ping", "data": {}}


def test_drop_oldest_on_overflow():
    async def run():
        bus = EventBus(max_queue=2)
        sub = bus.subscribe()
        # Register the queue without draining it.
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        # First event satisfies the pending __anext__ immediately.
        bus.publish("e", {"i": 0})
        first = await asyncio.wait_for(t, timeout=1)
        # Now fill the queue (cap 2) and overflow it.
        for i in range(1, 5):
            bus.publish("e", {"i": i})
        drained = []
        for _ in range(2):
            drained.append(await asyncio.wait_for(sub.__anext__(), timeout=1))
        await sub.aclose()
        return first, drained

    first, drained = asyncio.run(run())
    assert first == {"event": "e", "data": {"i": 0}}
    # Oldest (i=1, i=2) were dropped; newest two survive.
    assert [d["data"]["i"] for d in drained] == [3, 4]


def test_unsubscribe_on_close():
    async def run():
        bus = EventBus()
        sub = bus.subscribe()
        t = asyncio.ensure_future(sub.__anext__())
        await asyncio.sleep(0)
        assert bus.subscriber_count() == 1
        bus.publish("x")
        await asyncio.wait_for(t, timeout=1)
        await sub.aclose()
        return bus.subscriber_count()

    assert asyncio.run(run()) == 0


def test_sse_event_stream_preamble_and_frame():
    """The SSE framing helper emits the preamble then one frame per event."""
    from operator_api.routes import _sse_event_stream

    async def run():
        bus = EventBus()
        gen = _sse_event_stream(bus.subscribe, keepalive_s=5)
        preamble = await gen.__anext__()  # ": connected\n\n"
        # The next pull subscribes to the bus and blocks on q.get(); drive it on
        # a task so we can publish once it has registered.
        pull = asyncio.ensure_future(gen.__anext__())
        await asyncio.sleep(0.05)  # let the generator subscribe
        bus.publish("activity.message", {"text": "hi"})
        frame = await asyncio.wait_for(pull, timeout=2)
        await gen.aclose()
        return preamble, frame

    preamble, frame = asyncio.run(run())
    assert preamble == ": connected\n\n"
    assert frame == 'event: activity.message\ndata: {"text": "hi"}\n\n'


def test_sse_event_stream_emits_keepalive_when_idle():
    """With no events, the stream emits keepalive comments (holds the conn)."""
    from operator_api.routes import _sse_event_stream

    async def run():
        bus = EventBus()
        gen = _sse_event_stream(bus.subscribe, keepalive_s=0.05)
        await gen.__anext__()  # preamble
        ka = await asyncio.wait_for(gen.__anext__(), timeout=2)  # idle → keepalive
        await gen.aclose()
        return ka

    assert asyncio.run(run()) == ": keepalive\n\n"
