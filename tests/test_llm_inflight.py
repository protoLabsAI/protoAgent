"""Model calls acquire a per-lane in-flight slot when ``model.max_inflight`` is set
(ADR 0115 C4, #3760).

The limiter itself is proven in ``tests/test_llm_limiter.py``; this file proves the WIRING
in ``graph/llm.py`` and ``graph/providers/anthropic_oauth.py`` — with fakes at the model
boundary (``ChatOpenAI._astream``/``_agenerate`` and ``ChatAnthropic._astream``), no gateway:

- the slot is the OUTERMOST wrapper, acquired before the per-chunk stall guard and held until
  the stream is exhausted, errors, or is cancelled (D3);
- a reconnect releases before the backoff sleep and re-acquires with the ORIGINAL arrival (D5);
- queue wait never starts ``request_timeout`` (D4);
- a queue timeout raises ``GatewayQueueTimeout``, is not translated to ``StreamStallTimeout``,
  and is not retried (D4);
- ``_agenerate`` holds a slot too (D3);
- codex is covered through inheritance, anthropic-oauth through its own composition (D7);
- ``max_inflight: 0`` is a pure pass-through (no lane, no bookkeeping).
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

import graph.llm as llm
from graph import llm_limiter
from graph.llm import StreamStallTimeout, _guarded_reconnecting_stream
from graph.llm_limiter import GatewayQueueTimeout

_no_sleep = lambda _d: asyncio.sleep(0)  # noqa: E731 — skip reconnect backoff in tests


@pytest.fixture(autouse=True)
def _reset():
    llm_limiter._reset_for_tests()
    yield
    llm_limiter._reset_for_tests()


# ── fake model-boundary chunks (same shape as _chunk_has_content / _stream_measured) ──
class _Msg:
    def __init__(self, content=""):
        self.content = content
        self.additional_kwargs: dict = {}
        self.usage_metadata = None


class _Gen:
    def __init__(self, content=""):
        self.message = _Msg(content)


def _scripted(script):
    """A ``make_stream`` whose Nth open replays ``script[N] = (items, exc_or_None)``: yield
    each item, then raise ``exc`` if given. Returns ``(make_stream, state)`` so a test can
    assert how many times the stream was (re)opened."""
    state = {"opens": 0}

    def make_stream():
        idx = state["opens"]
        state["opens"] += 1
        items, exc = script[idx]

        async def gen():
            for it in items:
                yield it
            if exc is not None:
                raise exc

        return gen()

    return make_stream, state


# ── D3: the slot is the outermost wrapper, held until close/cancel ────────────────────
async def test_slot_is_held_until_the_stream_is_cancelled():
    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    started = asyncio.Event()
    release = asyncio.Event()

    def make():
        async def gen():
            started.set()
            await release.wait()  # a fake _astream that blocks, producing nothing
            yield _Gen("unreachable")

        return gen()

    async def consume():
        async for _ in _guarded_reconnecting_stream(
            make, timeout=None, max_retries=0, label="m", lane="L", sleep=_no_sleep
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), 2.0)  # the fake entered → the slot is already held

    kinds = [e.kind for e in events]
    assert kinds.count("acquired") == 1
    assert "released" not in kinds
    assert llm_limiter._LANES["L"].snapshot()["inflight"] == 1

    task.cancel()  # the consumer cancels mid-stream
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [e.kind for e in events].count("released") == 1
    assert llm_limiter._LANES["L"].snapshot()["inflight"] == 0


# ── D5: a reconnect releases before the backoff and re-acquires with the same arrival ──
async def test_reconnect_releases_and_reacquires_with_the_original_arrival(monkeypatch):
    llm_limiter.configure(limit=2, queue_timeout=30.0, interactive_reserve=0)

    real_acquire = llm_limiter.acquire
    arrivals: list = []

    def spy(lane, priority=None, *, arrival=None):
        arrivals.append(arrival)
        return real_acquire(lane, priority, arrival=arrival)

    monkeypatch.setattr(llm_limiter, "acquire", spy)

    events: list = []
    llm_limiter.add_listener(events.append)

    # Attempt 1: a transport drop before ANY content → retryable. Attempt 2: succeeds.
    make, state = _scripted([([], httpx.ReadError("drop")), ([_Gen("x"), _Gen("y")], None)])

    out = [
        c
        async for c in _guarded_reconnecting_stream(
            make, timeout=None, max_retries=2, label="m", lane="L", sleep=_no_sleep
        )
    ]

    assert [c.message.content for c in out] == ["x", "y"]
    assert state["opens"] == 2  # reconnected once
    assert len(arrivals) == 2  # one acquire per attempt
    assert arrivals[0] is not None and arrivals[0] == arrivals[1]  # arrival preserved (D5)
    kinds = [e.kind for e in events]
    assert kinds.count("acquired") == 2
    assert kinds.count("released") == 2  # released after the drop AND at the end


# ── D4: queue wait does not start request_timeout ─────────────────────────────────────
async def test_queue_wait_does_not_start_request_timeout():
    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)

    a_started = asyncio.Event()
    a_release = asyncio.Event()

    def make_a():
        async def gen():
            a_started.set()
            await a_release.wait()
            yield _Gen("a")

        return gen()

    b_opens: list = []

    def make_b():
        async def gen():
            b_opens.append(1)  # only runs once B holds a slot
            yield _Gen("b")

        return gen()

    async def consume_a():
        async for _ in _guarded_reconnecting_stream(
            make_a, timeout=None, max_retries=0, label="a", lane="L", sleep=_no_sleep
        ):
            pass

    async def consume_b():
        return [
            c.message.content
            async for c in _guarded_reconnecting_stream(
                make_b, timeout=0.1, max_retries=0, label="b", lane="L", sleep=_no_sleep
            )
        ]

    task_a = asyncio.create_task(consume_a())
    await asyncio.wait_for(a_started.wait(), 2.0)  # A holds the only slot

    task_b = asyncio.create_task(consume_b())
    await asyncio.sleep(0.35)  # far beyond B's 0.1s per-chunk guard, while B is still queued
    assert not task_b.done()  # B did NOT time out while waiting: its guard hasn't started
    assert b_opens == []  # B never opened its model stream (no slot yet)

    a_release.set()
    await asyncio.wait_for(task_a, 2.0)
    assert await asyncio.wait_for(task_b, 2.0) == ["b"]  # B acquired, then streamed in time
    assert b_opens == [1]


# ── D4: a queue timeout is GatewayQueueTimeout, not a stall, and is not retried ───────
async def test_queue_timeout_raises_gateway_queue_timeout_and_is_not_retried():
    llm_limiter.configure(limit=1, queue_timeout=0.1, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    a_started = asyncio.Event()
    a_release = asyncio.Event()

    def make_a():
        async def gen():
            a_started.set()
            await a_release.wait()
            yield _Gen("a")

        return gen()

    b_opens: list = []

    def make_b():
        b_opens.append(1)

        async def gen():
            yield _Gen("b")

        return gen()

    task_a = asyncio.create_task(
        _drain(_guarded_reconnecting_stream(
            make_a, timeout=None, max_retries=0, label="a", lane="L", sleep=_no_sleep
        ))
    )
    await asyncio.wait_for(a_started.wait(), 2.0)

    with pytest.raises(GatewayQueueTimeout) as ei:
        # max_retries=3: if the queue timeout were retryable it would rejoin the queue.
        async for _ in _guarded_reconnecting_stream(
            make_b, timeout=0.05, max_retries=3, label="b", lane="L", sleep=_no_sleep
        ):
            pass

    assert not isinstance(ei.value, StreamStallTimeout)  # never mislabelled as a stall
    assert b_opens == []  # acquisition failed first — the model stream never opened
    assert "L" in str(ei.value)  # the message names the lane
    assert [e.kind for e in events].count("timeout") == 1  # one attempt, not retried

    a_release.set()
    await asyncio.wait_for(task_a, 2.0)


async def _drain(stream):
    async for _ in stream:
        pass


# ── the pass-through of _lane_slot must be an ASYNC context (its result is always consumed
#    with `async with`): the un-laned caller AND a lane the call already holds. The sync
#    contextlib.nullcontext only grew async support in 3.10, so it would TypeError under
#    `async with` on older runtimes — this guards against reverting to it. ────────────────
async def test_lane_slot_pass_through_is_async_usable():
    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    # lane is None → un-laned caller (embeddings / raw httpx / acp:) → yields, no acquire.
    none_cm = llm._lane_slot(None)
    assert not isinstance(none_cm, contextlib.nullcontext)  # NOT the sync-only pass-through
    assert hasattr(none_cm, "__aenter__") and hasattr(none_cm, "__aexit__")
    async with none_cm:
        pass

    # a lane the logical call already holds (_HELD_LANES) → pass-through, reuse the outer slot.
    token = llm._HELD_LANES.set(frozenset({"L"}))
    try:
        held_cm = llm._lane_slot("L", arrival=123.0)
        assert not isinstance(held_cm, contextlib.nullcontext)
        assert hasattr(held_cm, "__aenter__") and hasattr(held_cm, "__aexit__")
        async with held_cm:
            pass
    finally:
        llm._HELD_LANES.reset(token)

    assert events == []  # neither pass-through touched the limiter
    assert llm_limiter._LANES == {}  # and no lane was ever created


# ── D3: the gateway client acquires on its (base_url, model) lane ─────────────────────
async def test_gateway_astream_acquires_on_its_lane(monkeypatch):
    from langchain_openai import ChatOpenAI

    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    async def fake_astream(self, *a, **k):
        yield _Gen("hi")

    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream, raising=True)

    client = llm._ReasoningChatOpenAI(
        model="protolabs/smart", api_key="k", base_url="http://gw/v1", timeout=100, max_retries=0
    )
    out = [c async for c in client._astream([])]

    assert [c.message.content for c in out] == ["hi"]
    acquired = [e for e in events if e.kind == "acquired"]
    assert len(acquired) == 1
    assert acquired[0].lane == client._lane_key()
    assert "protolabs/smart" in acquired[0].lane


# ── D3: non-streaming _agenerate holds a slot around the call ─────────────────────────
async def test_agenerate_holds_a_slot(monkeypatch):
    from langchain_openai import ChatOpenAI

    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    inflight_during: list = []

    async def fake_agenerate(self, *a, **k):
        inflight_during.append(llm_limiter._LANES[self._lane_key()].snapshot()["inflight"])
        return "RESULT"

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_agenerate, raising=True)

    client = llm._ReasoningChatOpenAI(model="m", api_key="k", base_url="http://gw/v1")
    assert await client._agenerate([]) == "RESULT"
    assert inflight_during == [1]  # the slot was held during the call
    assert [e.kind for e in events] == ["enqueued", "acquired", "released"]
    assert events[0].lane == client._lane_key()


# ── no double-acquire: a provider `_agenerate` that streams under the hood must reuse the
#    ONE slot, never take a second on the same lane (that would deadlock max_inflight: 1) ──
async def test_agenerate_delegating_to_astream_takes_one_slot(monkeypatch):
    from langchain_openai import ChatOpenAI

    # limit=1 so a genuine second acquire on the lane would BLOCK; a short queue timeout so a
    # regression fails fast as GatewayQueueTimeout instead of hanging the suite.
    llm_limiter.configure(limit=1, queue_timeout=0.5, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    # The pre-1.6 langchain_openai shape: BaseChatOpenAI._agenerate hands off to self._astream
    # when streaming=True. self._astream is our _ReasoningChatOpenAI._astream (wraps the lane);
    # its own super()._astream is the monkeypatched ChatOpenAI._astream below.
    async def fake_upstream_agenerate(self, *a, **k):
        return [c async for c in self._astream(*a, **k)][-1].message.content

    async def fake_astream(self, *a, **k):
        yield _Gen("streamed")

    monkeypatch.setattr(ChatOpenAI, "_agenerate", fake_upstream_agenerate, raising=True)
    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream, raising=True)

    client = llm._ReasoningChatOpenAI(model="m", api_key="k", base_url="http://gw/v1")
    # No deadlock and no GatewayQueueTimeout: the nested _astream acquire sees the lane already
    # held (_HELD_LANES) and passes through, reusing the outer _agenerate slot.
    result = await asyncio.wait_for(client._agenerate([]), 2.0)

    assert result == "streamed"
    assert [e.kind for e in events].count("acquired") == 1  # ONE slot, not two
    assert [e.kind for e in events].count("released") == 1
    assert llm_limiter._LANES[client._lane_key()].snapshot()["inflight"] == 0  # fully released


# ── D7: codex is covered through inheritance, with NO edit to the codex client ─────────
async def test_codex_subclass_acquires_through_inheritance(monkeypatch):
    from langchain_openai import ChatOpenAI

    from graph.providers.codex_client import CodexChatOpenAI

    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    async def fake_astream(self, *a, **k):
        yield _Gen("cx")

    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream, raising=True)

    client = CodexChatOpenAI(
        model="gpt-5-codex", api_key="k", base_url="https://chatgpt.com/backend-api/codex"
    )
    out = [c async for c in client._astream([])]

    assert [c.message.content for c in out] == ["cx"]
    acquired = [e for e in events if e.kind == "acquired"]
    assert len(acquired) == 1
    assert acquired[0].lane == client._lane_key()  # inherited (base_url, model) lane
    assert "gpt-5-codex" in acquired[0].lane


# ── D7: the anthropic-oauth client acquires on its `anthropic-oauth|<model>` lane ─────
def _skip_without_anthropic():
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic is not importable")
    return ao


async def test_anthropic_oauth_astream_acquires_on_its_lane(monkeypatch):
    ao = _skip_without_anthropic()
    from langchain_anthropic import ChatAnthropic

    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    async def fake_astream(self, *a, **k):
        yield _Gen("cl")

    monkeypatch.setattr(ChatAnthropic, "_astream", fake_astream, raising=True)
    monkeypatch.setattr(ao, "current_oauth_token", lambda **_k: "tok-abc")  # no keychain shell-out

    client = ao._OAuthChatAnthropic(
        model="claude-opus-5-5",
        api_key="oauth-via-auth-token",
        oauth_token="tok-abc",
        default_request_timeout=100,
        max_retries=0,
    )
    out = [c async for c in client._astream([])]

    assert [c.message.content for c in out] == ["cl"]
    acquired = [e for e in events if e.kind == "acquired"]
    assert len(acquired) == 1
    assert acquired[0].lane == "anthropic-oauth|claude-opus-5-5"


async def test_anthropic_oauth_agenerate_holds_a_slot(monkeypatch):
    ao = _skip_without_anthropic()
    from langchain_anthropic import ChatAnthropic

    llm_limiter.configure(limit=1, queue_timeout=30.0, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    async def fake_agenerate(self, *a, **k):
        return "R"

    monkeypatch.setattr(ChatAnthropic, "_agenerate", fake_agenerate, raising=True)
    monkeypatch.setattr(ao, "current_oauth_token", lambda **_k: "tok-abc")

    client = ao._OAuthChatAnthropic(
        model="claude-opus-5-5", api_key="oauth-via-auth-token", oauth_token="tok-abc"
    )
    assert await client._agenerate([]) == "R"
    assert [e.kind for e in events] == ["enqueued", "acquired", "released"]
    assert events[0].lane == "anthropic-oauth|claude-opus-5-5"


async def test_anthropic_oauth_agenerate_delegating_to_astream_takes_one_slot(monkeypatch):
    ao = _skip_without_anthropic()
    from langchain_anthropic import ChatAnthropic

    llm_limiter.configure(limit=1, queue_timeout=0.5, interactive_reserve=0)
    events: list = []
    llm_limiter.add_listener(events.append)

    async def fake_upstream_agenerate(self, *a, **k):
        return [c async for c in self._astream(*a, **k)][-1].message.content

    async def fake_astream(self, *a, **k):
        yield _Gen("cl")

    monkeypatch.setattr(ChatAnthropic, "_agenerate", fake_upstream_agenerate, raising=True)
    monkeypatch.setattr(ChatAnthropic, "_astream", fake_astream, raising=True)
    monkeypatch.setattr(ao, "current_oauth_token", lambda **_k: "tok-abc")

    client = ao._OAuthChatAnthropic(
        model="claude-opus-5-5",
        api_key="oauth-via-auth-token",
        oauth_token="tok-abc",
        default_request_timeout=100,
        max_retries=0,
    )
    result = await asyncio.wait_for(client._agenerate([]), 2.0)  # no deadlock / queue timeout

    assert result == "cl"
    assert [e.kind for e in events].count("acquired") == 1  # ONE slot on anthropic-oauth|…
    assert llm_limiter._LANES["anthropic-oauth|claude-opus-5-5"].snapshot()["inflight"] == 0


# ── max_inflight: 0 is a pure pass-through: no lane, no events, no bookkeeping ─────────
async def test_max_inflight_zero_is_a_pure_pass_through(monkeypatch):
    from langchain_openai import ChatOpenAI

    llm_limiter.configure(limit=0)  # the default: limiter off
    events: list = []
    llm_limiter.add_listener(events.append)

    async def fake_astream(self, *a, **k):
        yield _Gen("z")

    monkeypatch.setattr(ChatOpenAI, "_astream", fake_astream, raising=True)

    client = llm._ReasoningChatOpenAI(model="m", api_key="k", base_url="http://gw/v1")
    out = [c async for c in client._astream([])]

    assert [c.message.content for c in out] == ["z"]
    assert events == []  # nothing acquired/released
    assert llm_limiter._LANES == {}  # no lane ever created


# ── create_llm is the config→limiter bridge (read from `model.*` at build) ────────────
def test_create_llm_pushes_inflight_config_to_the_limiter():
    from graph.config import LangGraphConfig
    from graph.llm import create_llm

    cfg = LangGraphConfig(
        llm_max_inflight=5, llm_inflight_queue_timeout=42.0, llm_inflight_interactive_reserve=2
    )
    with contextlib.suppress(Exception):  # a keyless build may raise AFTER configure() runs
        create_llm(cfg)

    assert llm_limiter._LIMIT == 5
    assert llm_limiter._QUEUE_TIMEOUT == 42.0
    assert llm_limiter._RESERVE == 2
    assert llm_limiter.snapshot()["enabled"] is True
