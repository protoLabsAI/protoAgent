"""v0.3 compat JSON-RPC errors carry their real A2A codes (#3929).

a2a-sdk 1.1.0's v0.3 adapter wraps every handler error in -32603, so a classic
``tasks/get`` for a missing task read as an internal error while the v1 ``GetTask``
returned -32001. ``a2a_impl.v03_compat.create_a2a_jsonrpc_routes`` — the builder
server/__init__.py mounts — renders them through the SDK's own v1 mapping.
"""

from __future__ import annotations

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes.fastapi_routes import add_a2a_routes_to_fastapi
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentSkill
from fastapi import FastAPI
from fastapi.testclient import TestClient

import protolabs_a2a as pa
from a2a_impl.executor import ProtoAgentExecutor
from a2a_impl.v03_compat import create_a2a_jsonrpc_routes, harden_v03_error_codes


async def _never_stream(*_a, **_k):  # pragma: no cover — no turn runs in these tests
    raise AssertionError("no turn expected")
    yield


def _client() -> TestClient:
    card = pa.build_agent_card(
        name="test",
        description="d",
        url="http://test/a2a",
        version="0.0.0",
        skills=[AgentSkill(id="chat", name="chat", description="d", tags=["chat"])],
    )
    handler = DefaultRequestHandler(
        agent_executor=ProtoAgentExecutor(_never_stream),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = FastAPI()
    add_a2a_routes_to_fastapi(app, jsonrpc_routes=create_a2a_jsonrpc_routes(handler, rpc_url="/a2a"))
    return TestClient(app)


def test_v03_tasks_get_for_a_missing_task_is_task_not_found():
    c = _client()
    v03 = c.post(
        "/a2a",
        json={"jsonrpc": "2.0", "id": "r1", "method": "tasks/get", "params": {"id": "no-such-task"}},
    ).json()
    assert v03["id"] == "r1"
    assert v03["error"]["code"] == -32001, v03  # was -32603 (internal error)

    # Parity with the v1 path on the same endpoint.
    v1 = c.post(
        "/a2a",
        headers={"A2A-Version": "1.0"},
        json={"jsonrpc": "2.0", "id": "r2", "method": "GetTask", "params": {"id": "no-such-task"}},
    ).json()
    assert v1["error"]["code"] == v03["error"]["code"]


def test_harden_is_idempotent_and_tolerates_routes_without_an_adapter():
    class _Route:
        endpoint = staticmethod(lambda: None)

    assert harden_v03_error_codes([_Route()]) is False
    assert harden_v03_error_codes([]) is False
