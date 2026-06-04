"""Regression: the eval A2A client speaks the A2A 1.0 wire shape.

The A2A 1.0 migration (ADR 0014) swapped the hand-rolled handler for
``a2a-sdk`` (≥1.1), which serves **proto** method names (``SendMessage`` /
``GetTask`` / ``SendStreamingMessage`` / ``CancelTask``), gates every method on
an ``A2A-Version: 1.0`` request header (a missing header is read as 0.3 and the
1.0 methods 404 with ``-32601``), and emits untyped parts (``{"text": …}`` —
no ``kind`` discriminator) with ``TASK_STATE_*`` state names.

``evals/client.py`` was left on the 0.3 shape (``message/send`` + ``role:
"user"`` + ``{"kind": "text"}`` + no version header) and so failed *every* eval
case against a current server. These tests drive the real ``AgentClient``
against an in-process ``a2a-sdk`` app: a wrong method/role/header surfaces as
``-32601`` → ``state="failed"``, so a green round-trip is itself the assertion
that the client speaks 1.0. The companion negative case pins why the header is
load-bearing.
"""

from __future__ import annotations

import httpx
import pytest
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI

import protolabs_a2a as pa
import evals.client as ec
from a2a_executor import ProtoAgentExecutor, set_terminal_hook


async def _hello_stream(text, ctx, *, resume=False, caller_trace=None):
    """A minimal lead stream: some text + a usage frame → terminal."""
    yield ("text", "hello world")
    yield ("usage", {"input_tokens": 10, "output_tokens": 5, "cost_usd": 0.001})
    yield ("done", "hello world")


def _build_app(stream_fn) -> FastAPI:
    card = pa.build_agent_card(
        name="test", description="d", url="http://test/a2a", version="0.0.0",
        skills=[AgentSkill(id="chat", name="Chat", description="c", tags=["t"])],
        bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
    return app


@pytest.fixture(autouse=True)
def _no_terminal_hook():
    set_terminal_hook(None)
    yield
    set_terminal_hook(None)


@pytest.fixture
def routed_client(monkeypatch):
    """An ``AgentClient`` whose httpx calls route to an in-process a2a-sdk app."""
    app = _build_app(_hello_stream)
    orig = ec.httpx.AsyncClient

    def _patched(*a, **kw):
        kw["transport"] = httpx.ASGITransport(app=app)
        kw.setdefault("base_url", "http://test")
        return orig(*a, **kw)

    monkeypatch.setattr(ec.httpx, "AsyncClient", _patched)
    return ec.AgentClient(base_url="http://test"), app


def test_client_sets_the_version_header():
    # The single omission that 404'd every eval case against a 1.1 server.
    assert ec.AgentClient(base_url="http://test").headers["A2A-Version"] == "1.0"


@pytest.mark.asyncio
async def test_ask_round_trips_against_a2a_1_0(routed_client):
    client, _ = routed_client
    r = await client.ask("hi", timeout_s=5)
    assert r.state == "completed"          # 0.3 method/role → -32601 → "failed"
    assert r.text == "hello world"         # untyped {"text": …} part parsed
    assert r.usage.get("input_tokens") == 10  # cost-v1 DataPart parsed


@pytest.mark.asyncio
async def test_stream_round_trips_against_a2a_1_0(routed_client):
    client, _ = routed_client
    events, final = await client.stream("hi", timeout_s=5)
    kinds = {e["kind"] for e in events}
    # 1.0 SSE frames are oneof field names, not 0.3's hyphenated kinds.
    assert "statusUpdate" in kinds
    assert final is not None and final.state == "completed"
    assert final.text == "hello world"


@pytest.mark.asyncio
async def test_ask_with_context_id_round_trips(routed_client):
    # contextId is a field of Message in 1.0 — at params level it's a -32602.
    client, _ = routed_client
    r = await client.ask("hi", timeout_s=5, context_id="ctx-1")
    assert r.state == "completed"
    assert r.text == "hello world"


@pytest.mark.asyncio
async def test_params_level_context_id_is_rejected(routed_client):
    """Pins that contextId belongs inside the message, not on the request."""
    _, app = routed_client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5
    ) as c:
        r = await c.post("/a2a", headers={"A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": "x", "method": "SendMessage",
            "params": {"contextId": "ctx-1", "message": {
                "role": "ROLE_USER", "parts": [{"text": "hi"}], "messageId": "m"}},
        })
    assert r.json().get("error", {}).get("code") == -32602


@pytest.mark.asyncio
async def test_legacy_0_3_shape_is_rejected(routed_client):
    """Pins the contract: the old ``message/send`` (no version header) 404s, so
    the migration above is load-bearing, not cosmetic."""
    _, app = routed_client
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=5
    ) as c:
        r = await c.post("/a2a", json={
            "jsonrpc": "2.0", "id": "x", "method": "message/send",
            "params": {"message": {
                "role": "user", "parts": [{"kind": "text", "text": "hi"}], "messageId": "m"}},
        })
    assert r.json().get("error", {}).get("code") == -32601
