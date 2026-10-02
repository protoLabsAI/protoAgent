"""A send with ``contextId`` / ``taskId`` on ``params`` is refused, not silently re-sessioned.

a2a-sdk >= 1.2 ignores unknown request fields (a2aproject/a2a-python#1273), so a
params-level ``contextId`` used to start a FRESH session with no error: the agent forgot
the conversation and nothing said why. ``create_a2a_jsonrpc_routes`` (what
server/__init__.py mounts) wraps the route with :mod:`a2a_impl.request_guard`, which
answers -32602 naming the fix for exactly these keys, on both the v1 and the v0.3 send
methods. Everything else (message-level ids, other unknown fields, other methods) reaches
the SDK byte-for-byte.
"""

from __future__ import annotations

import httpx
import pytest
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI

import protolabs_a2a as pa
from a2a_impl.executor import ProtoAgentExecutor
from a2a_impl.request_guard import misplaced_id_keys
from a2a_impl.v03_compat import create_a2a_jsonrpc_routes

_V1 = {"A2A-Version": "1.0"}


def _app(seen: list) -> FastAPI:
    async def _stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        seen.append(ctx)
        yield ("text", "hello world")
        yield ("done", "hello world")

    card = pa.build_agent_card(
        name="test",
        description="d",
        url="http://test/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
        bearer=False,
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(_stream),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_a2a_jsonrpc_routes(handler, rpc_url="/a2a"))
    return app


def _v1_message(**extra) -> dict:
    return {"role": "ROLE_USER", "parts": [{"text": "hi"}], "messageId": "m1", **extra}


async def _post(app: FastAPI, payload: dict, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=10) as c:
        return await c.post("/a2a", json=payload, headers=headers or {})


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["contextId", "taskId", "context_id"])
@pytest.mark.parametrize("method", ["SendMessage", "SendStreamingMessage"])
async def test_v1_send_with_an_id_on_params_is_refused(method, key):
    seen: list = []
    r = await _post(
        _app(seen),
        {"jsonrpc": "2.0", "id": "x", "method": method, "params": {key: "ctx-1", "message": _v1_message()}},
        _V1,
    )
    body = r.json()
    assert body["id"] == "x"
    assert body["error"]["code"] == -32602, body
    assert key in body["error"]["message"] and "params.message" in body["error"]["message"]
    assert seen == []  # no turn ran, in a fresh context or any other


@pytest.mark.asyncio
async def test_v03_message_send_with_context_id_on_params_is_refused():
    seen: list = []
    r = await _post(
        _app(seen),
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "message/send",
            "params": {
                "contextId": "ctx-1",
                "message": {"role": "user", "parts": [{"kind": "text", "text": "hi"}], "messageId": "m1"},
            },
        },
    )
    body = r.json()
    assert body["id"] == 7
    assert body["error"]["code"] == -32602, body
    assert seen == []


@pytest.mark.asyncio
async def test_message_level_context_id_still_pins_the_session():
    seen: list = []
    r = await _post(
        _app(seen),
        {"jsonrpc": "2.0", "id": "x", "method": "SendMessage", "params": {"message": _v1_message(contextId="ctx-1")}},
        _V1,
    )
    body = r.json()
    assert "error" not in body, body
    assert body["result"]["task"]["contextId"] == "ctx-1"
    assert seen == ["ctx-1"]


@pytest.mark.asyncio
async def test_other_unknown_params_fields_still_pass_through():
    """Forward compatibility for genuinely new fields is upstream's call — keep it."""
    seen: list = []
    r = await _post(
        _app(seen),
        {
            "jsonrpc": "2.0",
            "id": "x",
            "method": "SendMessage",
            "params": {"someFutureField": {"a": 1}, "message": _v1_message(contextId="ctx-2")},
        },
        _V1,
    )
    body = r.json()
    assert "error" not in body, body
    assert seen == ["ctx-2"]


@pytest.mark.asyncio
async def test_non_send_methods_and_malformed_bodies_reach_the_sdk():
    app = _app([])
    # GetTask carries its own ``id`` on params — not a send, so the guard never looks.
    r = await _post(app, {"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": "nope"}}, _V1)
    assert r.json()["error"]["code"] == -32001  # the SDK's task-not-found, untouched

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post("/a2a", content=b"{not json", headers={"content-type": "application/json"})
    assert r.json()["error"]["code"] == -32700  # parse errors stay the SDK's


def test_misplaced_id_keys_only_looks_at_send_params():
    assert misplaced_id_keys({"method": "SendMessage", "params": {"contextId": "c", "taskId": "t"}}) == [
        "contextId",
        "taskId",
    ]
    assert misplaced_id_keys({"method": "SendMessage", "params": {"message": {"contextId": "c"}}}) == []
    assert misplaced_id_keys({"method": "GetTask", "params": {"contextId": "c"}}) == []
    assert misplaced_id_keys([{"method": "SendMessage", "params": {"contextId": "c"}}]) == []  # batches: SDK's
    assert misplaced_id_keys(None) == []


@pytest.mark.asyncio
async def test_a_well_formed_streaming_send_still_streams_through_the_guard():
    seen: list = []
    r = await _post(
        _app(seen),
        {
            "jsonrpc": "2.0",
            "id": "s",
            "method": "SendStreamingMessage",
            "params": {"message": _v1_message(contextId="ctx-3")},
        },
        _V1,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "hello world" in r.text
    assert seen == ["ctx-3"]
