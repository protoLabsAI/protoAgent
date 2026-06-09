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
    # Payload carries a monotonic seq (ADR 0039) alongside event/data.
    assert ra["event"] == "hello" and ra["data"] == {"n": 1} and ra["seq"] == 1
    assert rb["event"] == "hello" and rb["data"] == {"n": 1} and rb["seq"] == 1


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

    evt = asyncio.run(run())
    assert evt["event"] == "ping" and evt["data"] == {}


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
    assert first["event"] == "e" and first["data"] == {"i": 0}
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
    # Frame is an unnamed SSE event carrying the topic in the payload + the seq as the
    # SSE id (ADR 0039 — client routes by topic; id enables Last-Event-ID reconnect).
    assert frame == (
        'id: 1\ndata: {"topic": "activity.message", "data": {"text": "hi"}, "seq": 1}\n\n'
    )


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


# --- ADR 0039: topics, in-process handlers, ring replay, namespace guard ---


def test_topic_matches_wildcards():
    from events.bus import topic_matches

    assert topic_matches("#", "anything.at.all")
    assert topic_matches("artifact.created", "artifact.created")
    assert topic_matches("artifact.*", "artifact.created")
    assert not topic_matches("artifact.*", "artifact.created.again")  # * is one segment
    assert topic_matches("artifact.#", "artifact.created.again")      # # is the tail
    assert topic_matches("artifact.#", "artifact")                    # # matches empty tail
    assert not topic_matches("notes.*", "artifact.created")           # different namespace


def test_subscribe_handler_topic_filtered():
    """A handler only fires for matching topics; the seq/data arrive intact."""
    bus = EventBus()
    seen: list[dict] = []
    bus.subscribe_handler("artifact.*", lambda p: seen.append(p))
    bus.publish("artifact.created", {"id": "a1"})
    bus.publish("notes.changed", {"id": "n1"})  # different namespace — ignored
    assert [p["event"] for p in seen] == ["artifact.created"]
    assert seen[0]["data"] == {"id": "a1"} and seen[0]["seq"] == 1


def test_handler_exception_is_isolated():
    """A handler that raises can't break the publisher or other handlers."""
    bus = EventBus()
    good: list[str] = []

    def boom(_):
        raise RuntimeError("nope")

    bus.subscribe_handler("#", boom)
    bus.subscribe_handler("#", lambda p: good.append(p["event"]))
    bus.publish("x.y", {})  # must not raise
    assert good == ["x.y"]


def test_unsubscribe_handler():
    bus = EventBus()
    seen: list[str] = []
    off = bus.subscribe_handler("#", lambda p: seen.append(p["event"]))
    bus.publish("a.b")
    off()
    bus.publish("a.c")
    assert seen == ["a.b"]
    assert bus.handler_count() == 0


def test_ring_replay_since():
    """A reconnecting subscriber with ?since= replays missed events then streams live."""
    async def run():
        bus = EventBus()
        bus.publish("a.1", {"i": 1})
        bus.publish("a.2", {"i": 2})
        bus.publish("a.3", {"i": 3})
        sub = bus.subscribe(since=1)  # missed everything after seq 1
        replayed = [await asyncio.wait_for(sub.__anext__(), timeout=1) for _ in range(2)]
        await sub.aclose()
        return replayed

    replayed = asyncio.run(run())
    assert [p["seq"] for p in replayed] == [2, 3]


def test_registry_emit_namespaces_topic():
    """registry.emit auto-prefixes the plugin namespace (the no-cross-dep clause)."""
    from pathlib import Path
    from types import SimpleNamespace

    from graph.plugins.registry import PluginRegistry

    sent: list[tuple[str, dict]] = []
    reg = PluginRegistry("artifact", Path("."))
    reg.host = SimpleNamespace(publish=lambda t, d: sent.append((t, d)), on=None)  # isolate from HOST
    reg.emit("created", {"id": "a1"})            # bare → namespaced
    reg.emit("artifact.deleted", {"id": "x"})    # already namespaced → unchanged
    assert sent == [("artifact.created", {"id": "a1"}), ("artifact.deleted", {"id": "x"})]
