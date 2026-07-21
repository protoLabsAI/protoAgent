"""Client-side coverage for the two A2A 1.0 surfaces the eval client was missing:
``SubscribeToTask`` (reconnect to an in-flight task) and
``CreateTaskPushNotificationConfig`` (register a webhook).

Same harness idea as ``tests/test_eval_client_a2a_1_0.py``: build a **real**
in-process ``a2a-sdk`` app driven by ``ProtoAgentExecutor`` and route the eval
client's httpx calls at it, so a wrong method name or param shape surfaces as a
JSON-RPC error rather than passing vacuously.

Two wire facts these tests pin, both easy to get wrong from the 0.3 docs:

* ``SubscribeToTask`` params are exactly ``{"id": …}``
  (``SubscribeToTaskRequest`` = ``{tenant, id}``), and the server rejects a
  *terminal* task — resubscribe is a live re-attach, not a replay. Hence the
  gated agent stream: the task is parked mid-turn while we re-attach.
* ``CreateTaskPushNotificationConfig`` params are the **bare, flat**
  ``TaskPushNotificationConfig`` (``{tenant, id, task_id, url, token,
  authentication}`` — ``a2a_pb2.pyi:289``). There is no 0.3-style
  ``pushNotificationConfig`` wrapper in 1.0, and because the dispatcher
  ``ParseDict``s params strictly, sending one is a ``-32602``
  (``test_v0_3_nested_push_config_shape_is_rejected``).

The last test is a genuine end-to-end delivery: a real ``BasePushNotificationSender``
POSTs over real TCP to ``evals/webhook.py``'s listener, so ``capture.received``
is proof the agent actually called the webhook.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import (
    BasePushNotificationSender,
    InMemoryPushNotificationConfigStore,
    InMemoryTaskStore,
)
from a2a.types import AgentSkill
from fastapi import FastAPI

import protolabs_a2a as pa
import evals.client as ec
from a2a_impl.executor import ProtoAgentExecutor, set_terminal_hook
from evals.webhook import webhook_listener


def _gated_stream(gate: asyncio.Event):
    """An agent stream that parks mid-turn until ``gate`` is set.

    Keeps the task non-terminal (and its event queue open) long enough for a
    second client to re-attach to it.
    """

    async def _stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("text", "hello")
        await gate.wait()
        yield ("done", "hello world")

    return _stream


def _build_app(stream_fn, *, push_client: httpx.AsyncClient | None = None):
    """Return ``(app, handler)`` for an in-process a2a-sdk server.

    ``push_client`` opts the handler into real outbound push delivery — pass a
    genuine (unrouted) httpx client and notifications leave the process.
    """
    card = pa.build_agent_card(
        name="test",
        description="d",
        url="http://test/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="Chat", description="c", tags=["t"])],
        bearer=False,
    )
    push_store = InMemoryPushNotificationConfigStore()
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn),
        task_store=InMemoryTaskStore(),
        agent_card=card,
        push_config_store=push_store,
        push_sender=(BasePushNotificationSender(push_client, push_store) if push_client is not None else None),
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
    return app, handler


def _route(monkeypatch, app) -> ec.AgentClient:
    """Point ``evals.client``'s httpx at the in-process app."""
    orig = ec.httpx.AsyncClient

    def _patched(*a, **kw):
        kw["transport"] = httpx.ASGITransport(app=app)
        kw.setdefault("base_url", "http://test")
        return orig(*a, **kw)

    monkeypatch.setattr(ec.httpx, "AsyncClient", _patched)
    return ec.AgentClient(base_url="http://test")


async def _poll(predicate, *, timeout: float = 5.0, what: str = "condition"):
    """Await ``predicate()`` becoming truthy; return its value."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


def _live_task_id(handler) -> str | None:
    """The id of the one task the in-process server currently has running.

    ``DefaultRequestHandler`` is ``DefaultRequestHandlerV2`` in a2a-sdk ≥1.1 —
    in-flight tasks live in its ``ActiveTaskRegistry``, not a queue manager."""
    return next(iter(handler._active_task_registry._active_tasks), None)


def _subscriber_count(handler, task_id: str) -> int:
    """How many consumers are attached to the task's event stream."""
    active = handler._active_task_registry._active_tasks.get(task_id)
    return active._reference_count if active else 0


@pytest.fixture(autouse=True)
def _no_terminal_hook():
    set_terminal_hook(None)
    yield
    set_terminal_hook(None)


# ── SubscribeToTask ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_resubscribe_reattaches_to_an_in_flight_task(monkeypatch):
    """The dropped-stream recovery path: a second consumer re-attaches to a
    running task and rides it to a terminal state."""
    gate = asyncio.Event()
    app, handler = _build_app(_gated_stream(gate))
    client = _route(monkeypatch, app)

    streaming = asyncio.create_task(client.stream("hi", timeout_s=10))
    task_id = await _poll(lambda: _live_task_id(handler), what="the task to start")

    resub = asyncio.create_task(client.resubscribe(task_id, timeout_s=10))
    # Only release the agent once the resubscriber has actually tapped the queue —
    # a terminal task is an UnsupportedOperationError, not a replay.
    # (the original stream() consumer is subscriber #1, so wait for #2)
    await _poll(lambda: _subscriber_count(handler, task_id) >= 2, what="the resubscriber to attach")
    gate.set()

    events, final = await asyncio.wait_for(resub, 10)
    _, stream_final = await asyncio.wait_for(streaming, 10)

    kinds = [e["kind"] for e in events]
    # Spec §3.16: the first frame of a resubscribe is the Task snapshot.
    assert kinds[0] == "task"
    assert events[0]["result"].get("id") == task_id
    assert "statusUpdate" in kinds
    assert final is not None and final.state == "completed"
    assert final.task_id == task_id
    assert stream_final is not None and stream_final.state == "completed"


@pytest.mark.asyncio
async def test_resubscribe_to_an_unknown_task_reports_failure(monkeypatch):
    """A wrong id is a JSON-RPC error, not a hang: the client surfaces it rather
    than pretending the stream completed."""
    app, _ = _build_app(_gated_stream(asyncio.Event()))
    client = _route(monkeypatch, app)
    events, final = await client.resubscribe("no-such-task", timeout_s=5)
    assert final is None  # never reached a terminal frame
    assert not any(e["kind"] in ("statusUpdate", "artifactUpdate") for e in events)


# ── push notification config ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_push_config_round_trips(monkeypatch):
    """``CreateTaskPushNotificationConfig`` is accepted and the config is
    retrievable — the flat 1.0 param shape, proven against the real dispatcher."""
    gate = asyncio.Event()
    app, handler = _build_app(_gated_stream(gate))
    client = _route(monkeypatch, app)

    streaming = asyncio.create_task(client.stream("hi", timeout_s=10))
    try:
        task_id = await _poll(lambda: _live_task_id(handler), what="the task to start")
        created = await client.set_push_config(task_id, "https://example.test/hook", token="sekret")
        assert created["taskId"] == task_id
        assert created["url"] == "https://example.test/hook"
        assert created["token"] == "sekret"

        fetched = await client.get_push_config(task_id)
        assert fetched["url"] == "https://example.test/hook"

        listed = await client.list_push_configs(task_id)
        assert [c["url"] for c in listed] == ["https://example.test/hook"]
    finally:
        gate.set()
        await asyncio.wait_for(streaming, 10)


@pytest.mark.asyncio
async def test_v0_3_nested_push_config_shape_is_rejected(monkeypatch):
    """Pins why ``set_push_config`` sends flat params: the 0.3 wrapper
    (``{"taskId": …, "pushNotificationConfig": {…}}``) is a ``-32602`` here."""
    gate = asyncio.Event()
    app, handler = _build_app(_gated_stream(gate))
    _route(monkeypatch, app)  # so the client below shares the routed transport
    client = ec.AgentClient(base_url="http://test")

    streaming = asyncio.create_task(client.stream("hi", timeout_s=10))
    try:
        task_id = await _poll(lambda: _live_task_id(handler), what="the task to start")
        async with ec.httpx.AsyncClient(timeout=5) as c:
            r = await c.post(
                "http://test/a2a",
                headers=client.headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "x",
                    "method": "CreateTaskPushNotificationConfig",
                    "params": {
                        "taskId": task_id,
                        "pushNotificationConfig": {"url": "https://example.test/hook"},
                    },
                },
            )
        assert r.json().get("error", {}).get("code") == -32602
    finally:
        gate.set()
        await asyncio.wait_for(streaming, 10)


@pytest.mark.asyncio
async def test_push_notification_is_delivered_to_the_webhook_listener(monkeypatch):
    """End-to-end: register ``evals/webhook.py``'s listener as the task's
    callback, finish the task, and assert the agent really POSTed to it.

    The delivery is a genuine outbound HTTP request over loopback TCP — the push
    sender holds an unrouted httpx client (captured before the ASGI monkeypatch),
    so nothing about this assertion is simulated.
    """
    gate = asyncio.Event()
    async with webhook_listener() as (url, capture):
        async with httpx.AsyncClient(timeout=5) as push_client:
            app, handler = _build_app(_gated_stream(gate), push_client=push_client)
            client = _route(monkeypatch, app)

            streaming = asyncio.create_task(client.stream("hi", timeout_s=10))
            task_id = await _poll(lambda: _live_task_id(handler), what="the task to start")
            await client.set_push_config(task_id, url, token="tok-123")

            gate.set()
            _, final = await asyncio.wait_for(streaming, 10)
            assert final is not None and final.state == "completed"

            await _poll(lambda: capture.received, what="a push notification delivery")

    payloads = capture.received
    assert payloads, "no push notification was delivered"
    # Deliveries are the same stream-response envelopes the SSE frames carry.
    assert any((p.get("task") or p.get("statusUpdate") or {}) for p in payloads)
    ids = {(p.get("task") or {}).get("id") or (p.get("statusUpdate") or {}).get("taskId") for p in payloads}
    assert task_id in ids
    assert capture.headers[0].get("x-a2a-notification-token") == "tok-123"
