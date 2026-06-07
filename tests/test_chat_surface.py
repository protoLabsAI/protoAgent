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
    assert host.invoked == [("hi there", "fake:c9")]   # session = "<id>:<channel>"
    assert sent == ["RESPONSE"]


@pytest.mark.asyncio
async def test_handle_chunks_long_answers():
    reg, adapter, host = _wire({"enabled": True, "bot_token": "t"}, FakeHost(answer="A" * 25))
    reg.surface["start"](); await __import__("asyncio").sleep(0)
    sent = []
    await adapter.handle(InboundMessage("x", "u", "c", lambda s: _collect(sent, s)))
    assert len(sent) == 3 and all(len(p) <= 10 for p in sent)  # 25 / chunk_limit 10


@pytest.mark.asyncio
async def test_handle_admin_gating():
    reg, adapter, host = _wire({"enabled": True, "bot_token": "t", "admin_ids": ["123"]})
    reg.surface["start"](); await __import__("asyncio").sleep(0)
    sent = []
    await adapter.handle(InboundMessage("hi", "999", "c", lambda s: _collect(sent, s)))  # not admin
    assert host.invoked == [] and sent == []
    await adapter.handle(InboundMessage("hi", "123", "c", lambda s: _collect(sent, s)))  # admin
    assert len(host.invoked) == 1


@pytest.mark.asyncio
async def test_handle_invoke_error_replies_gracefully():
    host = FakeHost(); host.raise_exc = True
    reg, adapter, _ = _wire({"enabled": True, "bot_token": "t"}, host)
    reg.surface["start"](); await __import__("asyncio").sleep(0)
    sent = []
    await adapter.handle(InboundMessage("x", "u", "c", lambda s: _collect(sent, s)))
    assert len(sent) == 1 and "went wrong" in sent[0]


def test_start_skips_when_not_configured_or_disabled():
    reg, _, _ = _wire({"enabled": True})            # no token
    assert reg.surface["start"]() is None
    reg2, _, _ = _wire({"enabled": False, "bot_token": "t"})  # disabled
    assert reg2.surface["start"]() is None


async def _collect(bucket, s):
    bucket.append(s)
