"""Shared harness for tests that drive a real a2a-sdk app through ``ProtoAgentExecutor``.

One home for the app builder and its request helpers so test files never import from each
other (``from tests.test_a2a_handler import …`` coupled every A2A suite to that one file's
layout). Each consumer keeps its own per-test hygiene fixtures and calls
:func:`drain_handlers` at teardown.
"""

from __future__ import annotations

import asyncio

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.agent_card_routes import create_agent_card_routes
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.routes.jsonrpc_routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryPushNotificationConfigStore, InMemoryTaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI

import protolabs_a2a as pa
from a2a_impl.executor import ProtoAgentExecutor
from a2a_impl.registry import harden_active_task_registry

A2A_HEADERS = {"A2A-Version": "1.0"}

# Handlers built by build_app during the current test — drained at teardown (drain_handlers)
# so their retire-then-remove cleanup tasks (#1713) finish inside the test's event loop
# instead of being destroyed pending when pytest closes it.
HANDLERS: list = []


def skill() -> AgentSkill:
    return AgentSkill(id="chat", name="Chat", description="general chat", tags=["t"])


def build_app(stream_fn, *, bearer=None, api_key="", allowed_origins=None, task_store=None, **executor_kwargs):
    """Mount a real a2a-sdk app driven by ProtoAgentExecutor(stream_fn, **executor_kwargs)."""
    card = pa.build_agent_card(
        name="test",
        description="d",
        url="http://test/a2a",
        version="0.0.0",
        skills=[skill()],
        bearer=bool(bearer),
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(stream_fn, **executor_kwargs),
        task_store=task_store if task_store is not None else InMemoryTaskStore(),
        agent_card=card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )
    # Mirror production wiring (#1713): every test turn runs through the
    # owned-producer registry, exactly like server/__init__.py mounts it.
    assert harden_active_task_registry(handler)
    HANDLERS.append(handler)
    app = FastAPI()
    app.state.a2a_handler = handler
    if bearer is not None or api_key or allowed_origins is not None:
        from a2a_impl import auth

        auth.install(
            app,
            bearer_token=bearer or "",
            api_key=api_key,
            allowed_origins_raw=allowed_origins or "",
        )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/a2a"),
    )
    return app


def send_msg(client, text="hi", rpc_id="r1"):
    return client.post(
        "/a2a",
        headers=A2A_HEADERS,
        json={
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "SendMessage",
            "params": {"message": {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}]}},
        },
    )


async def poll_terminal(client, task_id, *, tries=60):
    for _ in range(tries):
        g = await client.post(
            "/a2a",
            headers=A2A_HEADERS,
            json={"jsonrpc": "2.0", "id": "g", "method": "GetTask", "params": {"id": task_id}},
        )
        t = g.json()["result"]
        if t["status"]["state"] in (
            "TASK_STATE_COMPLETED",
            "TASK_STATE_FAILED",
            "TASK_STATE_CANCELED",
            "TASK_STATE_INPUT_REQUIRED",
        ):
            return t
        await asyncio.sleep(0.03)
    raise AssertionError(f"task {task_id} never reached terminal")


async def drain_handlers() -> None:
    """Await every built handler's pending cleanup tasks, then forget the handlers."""
    for handler in HANDLERS:
        reg = getattr(handler, "_active_task_registry", None)
        tasks = set(getattr(reg, "_cleanup_tasks", ()) or ())
        if tasks:
            await asyncio.wait(tasks, timeout=5)
    HANDLERS.clear()
