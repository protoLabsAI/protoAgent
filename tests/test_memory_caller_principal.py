"""Memory delivery follows the caller's principal (ADR 0066 / ADR 0131).

A turn whose VERIFIED principal is the federation-token peer must not receive the
operator's prior-session digest, and must not have ``recall_session`` / ``session_search``
bound — both reach the operator's own past conversations on demand. Every other principal
keeps today's delivery: the shared operator bearer (the console), the fleet service token,
paired devices (kind ``device`` and kind ``agent``), and local/self-invocation turns.

The principal is decided server-side from the trust tier the real ``A2AAuthMiddleware``
stamps (surfaced in-graph through ``observability.tracing.current_trust_tier``), carried to
the turn by the same verified path ``tier_context_builder`` reads — never from request
metadata, a header the graph parsed, or graph-state input a caller can set. So every test
here drives delivery THROUGH the real auth middleware, authenticated by a real credential.

The federation test (``test_federation_turn_gets_no_digest_and_neither_recall_tool``) fails
on ``main`` — before the fix the digest is composed for the peer and both tools stay bound.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from langchain.agents.middleware.types import ModelRequest
from langchain_core.messages import HumanMessage
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from a2a_impl import auth
from a2a_impl.stores import tier_context_builder
from graph.middleware import memory as memmod
from graph.middleware.knowledge import KnowledgeMiddleware
from tools.memory_tools import _build_memory_tools

_CANARY = "CALLER-SCOPE-CANARY"


def _write_session(memory_dir, session_id: str, topic: str, *, gist: str = "") -> None:
    doc = {
        "session_id": session_id,
        "trace_id": f"trace-{session_id}",
        "messages": [{"role": "user", "content": topic}],
        "final_output": f"done: {topic}",
        "timestamp": "2026-08-28T12:00:00+00:00",
    }
    if gist:
        doc["gist"] = gist
    (memory_dir / memmod.session_filename(session_id)).write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    """A tmp session-memory dir, quiet working state, and isolated auth + device state."""
    monkeypatch.setenv("MEMORY_PATH", str(tmp_path))
    # Leave only the prior-session digest as a possible source of the canary.
    monkeypatch.setattr("graph.projection.working_state_block", lambda state: "")
    # Snapshot the auth credential holders so ``auth.configure`` can't leak across tests.
    for name in ("_BEARER", "_FEDERATION", "_FLEET", "_API_KEY", "_ALLOWED_ORIGINS", "_MEMBER_PUBLIC"):
        monkeypatch.setattr(auth, name, list(getattr(auth, name)))
    from security import devices

    monkeypatch.setattr(devices, "_registry_path", lambda: tmp_path / "devices.json")
    return tmp_path


def _deliver(headers: dict, *, session_id: str = "peer-turn") -> dict:
    """Run the memory projection inside a route behind the REAL ``A2AAuthMiddleware``,
    authenticated by ``headers``. Returns the delivered context text, the bound tool
    names, and the server-verified principal the turn ran as."""
    captured: dict = {}

    async def turn(request):
        verified = tier_context_builder().build(request).state
        captured["principal_kind"] = verified["principal_kind"]
        captured["trust_tier"] = verified["trust_tier"]
        tools = _build_memory_tools(None)
        mw = KnowledgeMiddleware(None, bound_tool_names=frozenset(t.name for t in tools))
        state = {"session_id": session_id, "messages": [HumanMessage(content="hello")], "incognito": False}
        state.update(mw.before_agent(state, None))
        model_request = ModelRequest(model=None, messages=state["messages"], tools=tools, state=state, runtime=None)
        delivered = mw.wrap_model_call(model_request, lambda req: req)
        return JSONResponse(
            {
                "context": "\n".join(str(m.content) for m in delivered.messages),
                "tools": [t.name for t in delivered.tools],
            }
        )

    app = Starlette(routes=[Route("/a2a", turn)])
    app.add_middleware(auth.A2AAuthMiddleware)
    with TestClient(app) as client:
        response = client.get("/a2a", headers=headers)
    assert response.status_code == 200
    return {**response.json(), **captured}


def test_federation_turn_gets_no_digest_and_neither_recall_tool(memory_env):
    """The confirmed exposure, fixed: a federation-token peer receives no prior-session
    digest and has neither session-recall tool bound. Fails on ``main`` (digest composed,
    tools bound)."""
    _write_session(memory_env, "operator-session", _CANARY)
    auth.configure(
        bearer_token="operator-test", federation_token="federation-test", api_key="", allowed_origins_raw=""
    )

    result = _deliver({"Authorization": "Bearer federation-test"})

    # Decided from the server-verified principal, not anything the caller sent.
    assert result["principal_kind"] == "federation"
    assert result["trust_tier"] == "federation"
    # No operator prior-session digest reached the peer, and neither recall tool is bound.
    assert _CANARY not in result["context"]
    assert "recall_session" not in result["tools"]
    assert "session_search" not in result["tools"]
    # Only the two session-recall tools are pulled — the rest of the memory surface stays.
    assert "memory_search" in result["tools"]


def test_operator_bearer_turn_keeps_the_digest_and_both_tools(memory_env):
    """The shared operator bearer (the console) is unchanged: digest present, both tools."""
    _write_session(memory_env, "operator-session", _CANARY)
    auth.configure(
        bearer_token="operator-test", federation_token="federation-test", api_key="", allowed_origins_raw=""
    )

    result = _deliver({"Authorization": "Bearer operator-test"})

    assert result["principal_kind"] == "shared-operator-bearer"
    assert result["trust_tier"] == "operator"
    assert _CANARY in result["context"]
    assert {"recall_session", "session_search"} <= set(result["tools"])


def test_fleet_token_turn_is_unchanged(memory_env):
    """The fleet service token (ADR 0089) resolves to the operator tier — unchanged."""
    _write_session(memory_env, "operator-session", _CANARY)
    auth.configure(
        bearer_token="operator-test",
        federation_token="federation-test",
        fleet_token="fleet-test",
        api_key="",
        allowed_origins_raw="",
    )

    result = _deliver({"Authorization": "Bearer fleet-test"})

    assert result["principal_kind"] == "fleet-service"
    assert result["trust_tier"] == "operator"
    assert _CANARY in result["context"]
    assert {"recall_session", "session_search"} <= set(result["tools"])


@pytest.mark.parametrize("kind", ["device", "agent"])
def test_paired_device_turn_is_unchanged(memory_env, kind):
    """A paired device (kind ``device`` or ``agent``) is the operator tier — unchanged."""
    from security import devices

    _write_session(memory_env, "operator-session", _CANARY)
    auth.configure(
        bearer_token="operator-test", federation_token="federation-test", api_key="", allowed_origins_raw=""
    )
    code, _ = devices.start_pairing(kind=kind)
    _, token = devices.claim_pairing(code, "paired device")

    result = _deliver({"Authorization": f"Bearer {token}"})

    assert result["trust_tier"] == "operator"
    assert _CANARY in result["context"]
    assert {"recall_session", "session_search"} <= set(result["tools"])


def test_federation_turn_receives_no_prior_session_gist(memory_env):
    """A chat's gist rides the prior-session digest only (ADR 0132 S5); with the digest
    gated off the federation peer never sees it — the operator still does."""
    _write_session(memory_env, "gisted-session", "a prior topic", gist="GIST-CANARY")
    auth.configure(
        bearer_token="operator-test", federation_token="federation-test", api_key="", allowed_origins_raw=""
    )

    assert "GIST-CANARY" in _deliver({"Authorization": "Bearer operator-test"})["context"]
    assert "GIST-CANARY" not in _deliver({"Authorization": "Bearer federation-test"})["context"]


def test_federation_turn_receives_no_closed_chat_card(memory_env):
    """A closed-chat card rides the prior-session digest only (ADR 0132); the federation
    peer never sees it once the digest is gated off — the operator still does."""
    memmod.record_closed_session(
        "closed-session",
        topic="CLOSEDCARD-CANARY",
        gist="g",
        closed_at=datetime.now(timezone.utc).isoformat(),
    )
    auth.configure(
        bearer_token="operator-test", federation_token="federation-test", api_key="", allowed_origins_raw=""
    )

    assert "CLOSEDCARD-CANARY" in _deliver({"Authorization": "Bearer operator-test"})["context"]
    assert "CLOSEDCARD-CANARY" not in _deliver({"Authorization": "Bearer federation-test"})["context"]
