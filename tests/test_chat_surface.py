"""The communication-plugin standard — ChatAdapter + register_chat_surface (ADR 0029)."""

from __future__ import annotations

import pytest

from graph.plugins.chat_surface import InboundMessage, _chunk, register_chat_surface


# --- fakes -------------------------------------------------------------------


class FakeHost:
    def __init__(self, answer="RESPONSE"):
        self.answer = answer
        self.invoked: list[tuple] = []
        self.publish = None
        self.subscribe = None
        self.raise_exc = False

    async def invoke(self, prompt, session_id):
        self.invoked.append((prompt, session_id))
        if self.raise_exc:
            raise RuntimeError("boom")
        return self.answer


class FakeAdapter:
    id = "fake"
    chunk_limit = 10

    def __init__(self):
        self.handle = None

    def configured(self, cfg):
        return bool(cfg.get("bot_token"))

    async def validate(self, cfg):
        return (True, "fakebot", None) if cfg.get("bot_token") else (False, None, "no token")

    async def run(self, handle, *, cfg, host):
        self.handle = handle  # capture so tests can drive it


class FakeRegistry:
    def __init__(self, config, host):
        self.config = config
        self.host = host
        self.surface = None
        self.routers = []
        self.tools = []

    def register_surface(self, start, stop=None, name=None, reload=None):
        self.surface = {"start": start, "stop": stop, "reload": reload, "name": name}

    def register_router(self, router, prefix=""):
        self.routers.append((router, prefix))

    def register_tools(self, tools):
        self.tools.extend(tools)


def _wire(config, host=None):
    host = host or FakeHost()
    adapter = FakeAdapter()
    reg = FakeRegistry(config, host)
    register_chat_surface(reg, adapter)
    return reg, adapter, host


# --- chunking ----------------------------------------------------------------


def test_chunk_respects_limit_and_prefers_boundaries():
    assert _chunk("", 10) == []
    assert _chunk("short", 10) == ["short"]
    parts = _chunk("hello world foo bar baz", 12)
    assert all(len(p) <= 12 for p in parts) and "".join(p.replace(" ", "") for p in parts) == "helloworldfoobarbaz"
    assert _chunk("abc", 0) == ["abc"]  # no chunking


# --- wiring ------------------------------------------------------------------


def test_registers_surface_test_route_and_no_tools():
    reg, _, _ = _wire({"enabled": True, "bot_token": "t"})
    assert reg.surface and reg.surface["name"] == "fake-gateway"
    paths = [r.path for (router, _p) in reg.routers for r in router.routes]
    assert "/api/config/test-fake" in paths
    assert reg.tools == []


# --- handle glue (admin-gate, session key, invoke, chunked reply) ------------


@pytest.mark.asyncio
async def test_handle_invokes_and_replies_with_session_key():
    reg, adapter, host = _wire({"enabled": True, "bot_token": "t"})
    reg.surface["start"]()
    await __import__("asyncio").sleep(0)  # let run() capture handle
    sent = []
    await adapter.handle(InboundMessage("hi there", "u1", "c9", lambda s: _collect(sent, s)))
    assert host.invoked == [("hi there", "fake:c9")]  # session = "<id>:<channel>"
    assert sent == ["RESPONSE"]


@pytest.mark.asyncio
async def test_handle_chunks_long_answers():
    reg, adapter, host = _wire({"enabled": True, "bot_token": "t"}, FakeHost(answer="A" * 25))
    reg.surface["start"]()
    await __import__("asyncio").sleep(0)
    sent = []
    await adapter.handle(InboundMessage("x", "u", "c", lambda s: _collect(sent, s)))
    assert len(sent) == 3 and all(len(p) <= 10 for p in sent)  # 25 / chunk_limit 10


@pytest.mark.asyncio
async def test_handle_admin_gating():
    reg, adapter, host = _wire({"enabled": True, "bot_token": "t", "admin_ids": ["123"]})
    reg.surface["start"]()
    await __import__("asyncio").sleep(0)
    sent = []
    await adapter.handle(InboundMessage("hi", "999", "c", lambda s: _collect(sent, s)))  # not admin
    assert host.invoked == [] and sent == []
    await adapter.handle(InboundMessage("hi", "123", "c", lambda s: _collect(sent, s)))  # admin
    assert len(host.invoked) == 1


@pytest.mark.asyncio
async def test_handle_invoke_error_replies_gracefully():
    host = FakeHost()
    host.raise_exc = True
    reg, adapter, _ = _wire({"enabled": True, "bot_token": "t"}, host)
    reg.surface["start"]()
    await __import__("asyncio").sleep(0)
    sent = []
    await adapter.handle(InboundMessage("x", "u", "c", lambda s: _collect(sent, s)))
    assert len(sent) == 1 and "went wrong" in sent[0]


def test_start_skips_when_not_configured_or_disabled():
    reg, _, _ = _wire({"enabled": True})  # no token
    assert reg.surface["start"]() is None
    reg2, _, _ = _wire({"enabled": False, "bot_token": "t"})  # disabled
    assert reg2.surface["start"]() is None


async def _collect(bucket, s):
    bucket.append(s)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_decision_notice_uses_owner_hook_not_inbound_replies(monkeypatch, legacy):
    import asyncio

    from graph.decisions import DecisionStore

    monkeypatch.setattr("graph.sdk.console_url", lambda: "https://console.example")
    monkeypatch.setattr("security.public_console.public_origin", lambda: "")
    reg, adapter, _ = _wire({"enabled": True, "bot_token": "t"})
    notices, replies = [], []
    delivered = asyncio.Event()

    async def typed(text, link, *, kind, importance):
        notices.append((text, link, kind, importance))
        delivered.set()

    async def old(text, link):
        notices.append((text, link))
        delivered.set()

    # The transport's owner hook is distinct from per-user/group reply callbacks.
    adapter.notify = old if legacy else typed
    register_chat_surface(reg, adapter)
    try:
        await reg.surface["start"]()
        for user, channel in [("owner", "owner-dm"), ("other", "group")]:
            await adapter.handle(InboundMessage("hello", user, channel, lambda s: _collect(replies, s)))
        replies.clear()
        store = DecisionStore(":memory:")
        record = store.create("fake:group", "Continue?", ["Yes"], thread_id="t", interrupt_id="i")
        assert store.create("fake:group", "Continue?", ["Yes"], thread_id="t", interrupt_id="i").id == record.id
        await asyncio.wait_for(delivered.wait(), 2)
        await asyncio.sleep(0)
        expected = (
            "Agent needs a decision: Continue?\n- Yes\nOpen this decision in the console to answer.",
            "https://console.example/app/?session=fake%3Agroup",
        )
        assert notices == [expected if legacy else (*expected, "question", "high")]
        assert replies == []  # No unsolicited delivery to an inbound chat, including the decision's chat.
    finally:
        await reg.surface["stop"]()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,importance", [("notify", "low"), ("review", "high")])
async def test_sync_typed_hook_and_failure_are_not_retried(kind, importance, caplog):
    import asyncio
    import threading

    from graph.plugins.chat_surface import notify_chat_surfaces

    reg, adapter, _ = _wire({"enabled": True, "bot_token": "t"})
    calls = []
    loop_thread = threading.get_ident()
    completed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def notify(text, link, *, kind, importance):
        calls.append((text, link, kind, importance, threading.get_ident()))
        loop.call_soon_threadsafe(completed.set)
        raise TypeError("transport failed after sending")

    adapter.notify = notify
    register_chat_surface(reg, adapter)
    try:
        await reg.surface["start"]()
        await asyncio.to_thread(
            notify_chat_surfaces, "Review the result in the console.", "/app/", kind=kind, importance=importance
        )
        await asyncio.wait_for(completed.wait(), 2)
        # A callback queued from the worker can precede completion of the hook.
        async with asyncio.timeout(2):
            while "transport failed after sending" not in caplog.text:
                await asyncio.sleep(0)
        assert len(calls) == 1
        assert calls[0][:4] == ("Review the result in the console.", "/app/", kind, importance)
        assert calls[0][4] != loop_thread
    finally:
        await reg.surface["stop"]()
