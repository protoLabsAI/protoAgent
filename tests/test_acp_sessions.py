"""GET /api/acp/sessions (#2889) — read surface for the live ACP coding-agent
registry (ADR 0033 slice 4): thread → runtime, busy refcount, last access."""

from __future__ import annotations

import asyncio
import importlib
import time
import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2a_impl import auth


def _chat_module():
    return importlib.import_module("server.chat")  # the `server.chat` attr is the re-exported fn


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Fresh registry dicts + lock per test — never touch (or leak into) module state."""
    chat = _chat_module()
    monkeypatch.setattr(chat, "_ACP_RUNTIMES", {})
    monkeypatch.setattr(chat, "_ACP_RUNTIME_ACCESS", {})
    monkeypatch.setattr(chat, "_ACP_BUSY", {})
    monkeypatch.setattr(chat, "_ACP_LOCK", asyncio.Lock())


@pytest.fixture(autouse=True)
def _reset_auth():
    """Each test seeds the auth guard itself; reset module state around it."""
    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._FLEET[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None
    auth._MEMBER_PUBLIC[0] = None
    yield
    auth._BEARER[0] = None
    auth._FEDERATION[0] = None
    auth._FLEET[0] = None
    auth._API_KEY[0] = ""
    auth._ALLOWED_ORIGINS[0] = None
    auth._MEMBER_PUBLIC[0] = None


def _client() -> TestClient:
    """A minimal app with the route wired the way server/__init__.py wires it,
    behind the same default-deny auth middleware that gates /api/ in production."""
    chat = _chat_module()
    app = FastAPI()

    @app.get("/api/acp/sessions")
    async def _acp_sessions():
        return await chat.acp_sessions_snapshot()

    app.add_middleware(auth.A2AAuthMiddleware)
    return TestClient(app)


def test_empty_registry_returns_an_empty_list():
    r = _client().get("/api/acp/sessions")
    assert r.status_code == 200
    assert r.json() == []


def test_snapshot_shape_with_a_mocked_runtime():
    chat = _chat_module()
    now = time.monotonic()
    chat._ACP_RUNTIMES["t-busy"] = types.SimpleNamespace(agent="proto")
    chat._ACP_RUNTIME_ACCESS["t-busy"] = now - 42.7
    chat._ACP_BUSY["t-busy"] = 1  # in-flight turn
    chat._ACP_RUNTIMES["t-idle"] = types.SimpleNamespace(agent="codex")
    chat._ACP_RUNTIME_ACCESS["t-idle"] = now

    r = _client().get("/api/acp/sessions")
    assert r.status_code == 200
    by_tid = {s["thread_id"]: s for s in r.json()}
    assert set(by_tid) == {"t-busy", "t-idle"}

    busy = by_tid["t-busy"]
    assert set(busy) == {"thread_id", "agent", "busy", "last_access_s_ago"}
    assert busy["agent"] == "proto"
    assert busy["busy"] is True
    assert isinstance(busy["last_access_s_ago"], float)
    assert busy["last_access_s_ago"] == pytest.approx(42.7, abs=1.0)

    idle = by_tid["t-idle"]
    assert idle["agent"] == "codex"
    assert idle["busy"] is False  # no refcount entry → idle
    assert idle["last_access_s_ago"] == pytest.approx(0.0, abs=1.0)

    # Observing must not keep a session alive: the snapshot never bumps access.
    assert chat._ACP_RUNTIME_ACCESS["t-busy"] == now - 42.7


def test_route_is_bearer_gated():
    auth.configure(bearer_token="secret", api_key="", allowed_origins_raw="")
    c = _client()
    assert c.get("/api/acp/sessions").status_code == 401  # default-deny under /api/
    ok = c.get("/api/acp/sessions", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
    assert ok.json() == []


async def test_snapshot_is_taken_under_the_registry_lock():
    chat = _chat_module()
    async with chat._ACP_LOCK:  # a concurrent turn holds the lock to mutate the registry
        snap = asyncio.create_task(chat.acp_sessions_snapshot())
        await asyncio.sleep(0.02)
        assert not snap.done()  # waits for the lock instead of reading a torn registry
    assert await snap == []
