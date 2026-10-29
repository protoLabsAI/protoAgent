"""Raw model passthrough — POST /api/models/{connection}/v1/chat/completions (#4608 part 1).

The surface is a PURE model call: it builds the connection's model with ``create_llm`` on the
qualified ``<connection>:<model>`` slot (ADR 0106), runs one OpenAI-shaped chat call, and converts
in and out of the OpenAI wire shape. No graph, no tools execute, no SOUL, no memory.

Fakes sit at the model-BUILDER boundary (``graph.llm._build_gateway_llm`` /
``graph.providers.build_native_oauth_llm``), so ``create_llm``'s real qualified-slot dispatch runs
for every connection TYPE — gateway, local openai-compat, native subscription — while no network
call is made. The fake is a real ``BaseChatModel`` so the usage callback the route attaches is fed
exactly as a live gateway would feed it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from graph.config import LangGraphConfig
from operator_api.model_passthrough_routes import (
    aimessage_to_openai_message,
    build_chat_completion,
    openai_messages_to_langchain,
    register_model_passthrough_routes,
)
from runtime.state import STATE

PATH = "/api/models/{conn}/v1/chat/completions"

DOC = {
    "providers": [
        {"id": "prod-gateway", "type": "openai-compat", "base_url": "https://prod/v1"},
        {"id": "local-vllm", "type": "openai-compat", "base_url": "http://localhost:8000/v1"},
        {"id": "claude-sub", "type": "anthropic-oauth"},
    ],
    "model": {"name": "prod-gateway:protolabs/reasoning"},
}


class _FakeChat(BaseChatModel):
    """A real ``BaseChatModel`` (so the usage callback fires) that returns a preset reply and
    records what it was asked — the messages it saw and any per-call / tool bindings."""

    response: Any = None
    captured: dict = Field(default_factory=dict)

    @property
    def _llm_type(self) -> str:
        return "fake-passthrough"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.captured["messages"] = messages
        self.captured["kwargs"] = kwargs
        return ChatResult(generations=[ChatGeneration(message=self.response)])

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        self.captured["tools"] = tools
        self.captured["tool_choice"] = tool_choice
        return self


def _reply(content="", *, tool_calls=None, input_tokens=11, output_tokens=7):
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        usage_metadata={
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        response_metadata={"model_name": "fake-model"},
    )


class _FakeStore:
    def __init__(self):
        self.rows: list[dict] = []

    def record(self, row):
        self.rows.append(row)


@pytest.fixture
def harness(monkeypatch):
    """App + config + builder fakes + a capturing telemetry store, driven from a loopback client."""
    cfg = LangGraphConfig.from_dict(DOC)
    monkeypatch.setattr(STATE, "graph_config", cfg, raising=False)
    store = _FakeStore()
    monkeypatch.setattr(STATE, "telemetry_store", store, raising=False)

    state = {"response": _reply("ok"), "llm": None, "calls": []}

    def _gateway_builder(config, model_name=None, reasoning_effort=None, provider=None):
        state["calls"].append(("gateway", model_name, getattr(provider, "id", None)))
        llm = _FakeChat(response=state["response"])
        state["llm"] = llm
        return llm

    def _native_builder(ptype, config, model_name=None, reasoning_effort=None):
        state["calls"].append(("native", ptype, model_name))
        llm = _FakeChat(response=state["response"])
        state["llm"] = llm
        return llm

    monkeypatch.setattr("graph.llm._build_gateway_llm", _gateway_builder)
    monkeypatch.setattr("graph.providers.build_native_oauth_llm", _native_builder)

    app = FastAPI()
    register_model_passthrough_routes(app)
    client = TestClient(app, client=("127.0.0.1", 5000))

    return type("H", (), {"client": client, "store": store, "state": state, "cfg": cfg})()


def _post(harness, conn, body):
    return harness.client.post(PATH.format(conn=conn), json=body)


# ── pure converters (r2) ─────────────────────────────────────────────────────────────────────


def test_openai_messages_convert_every_role():
    lc = openai_messages_to_langchain(
        [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city": "SF"}'}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "72F"},
        ]
    )
    assert [type(m) for m in lc] == [SystemMessage, HumanMessage, AIMessage, ToolMessage]
    # the assistant tool call parsed its JSON-string arguments into a dict
    assert lc[2].tool_calls == [{"name": "get_weather", "args": {"city": "SF"}, "id": "call_1", "type": "tool_call"}]
    assert lc[3].tool_call_id == "call_1" and lc[3].content == "72F"


def test_malformed_tool_arguments_degrade_to_empty_dict():
    lc = openai_messages_to_langchain(
        [{"role": "assistant", "content": "", "tool_calls": [{"id": "x", "function": {"name": "f", "arguments": "not json"}}]}]
    )
    assert lc[0].tool_calls[0]["args"] == {}


def test_aimessage_to_openai_tool_calls_shape():
    msg = _reply("", tool_calls=[{"name": "lookup", "args": {"q": "x"}, "id": "call_9", "type": "tool_call"}])
    out = aimessage_to_openai_message(msg)
    assert out["role"] == "assistant"
    tc = out["tool_calls"][0]
    assert tc["id"] == "call_9"
    assert tc["type"] == "function"
    assert tc["function"]["name"] == "lookup"
    assert tc["function"]["arguments"] == '{"q": "x"}'  # arguments is a JSON STRING


def test_build_chat_completion_finish_reason():
    tool = build_chat_completion(
        _reply("", tool_calls=[{"name": "f", "args": {}, "id": "c", "type": "tool_call"}]),
        model="m",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    )
    assert tool["object"] == "chat.completion"
    assert tool["choices"][0]["finish_reason"] == "tool_calls"
    plain = build_chat_completion(_reply("hello"), model="m", usage={})
    assert plain["choices"][0]["finish_reason"] == "stop"
    assert plain["choices"][0]["message"]["content"] == "hello"


# ── the three connection types, each via create_llm's qualified-slot path (r1) ─────────────────


def test_gateway_connection_builds_via_qualified_slot(harness):
    harness.state["response"] = _reply("gateway reply")
    r = _post(harness, "prod-gateway", {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "gateway reply"
    assert body["choices"][0]["finish_reason"] == "stop"
    # built through the openai-compat (gateway) lane, with the bare model off the qualified slot
    assert harness.state["calls"] == [("gateway", "gpt-4o", "prod-gateway")]


def test_local_openai_compat_connection_builds_via_qualified_slot(harness):
    r = _post(harness, "local-vllm", {"model": "qwen3-32b", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    assert harness.state["calls"] == [("gateway", "qwen3-32b", "local-vllm")]


def test_anthropic_oauth_connection_builds_via_native_lane(harness):
    r = _post(harness, "claude-sub", {"model": "claude-sonnet-4-6", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200, r.text
    # the native subscription lane (ADR 0097), NOT the gateway builder
    assert harness.state["calls"] == [("native", "anthropic-oauth", "claude-sonnet-4-6")]


# ── tool-call round trip + plain reply (r2) ────────────────────────────────────────────────────


def test_tools_produce_openai_tool_calls_and_finish_reason(harness):
    harness.state["response"] = _reply(
        "", tool_calls=[{"name": "get_weather", "args": {"city": "SF"}, "id": "call_1", "type": "tool_call"}]
    )
    body = {
        "model": "gpt-4o",
        "messages": [{"role": "user", "content": "weather in SF?"}],
        "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
        "tool_choice": "auto",
    }
    r = _post(harness, "prod-gateway", body)
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    tc = choice["message"]["tool_calls"][0]
    assert tc["id"] == "call_1" and tc["type"] == "function"
    assert tc["function"]["name"] == "get_weather"
    assert tc["function"]["arguments"] == '{"city": "SF"}'
    # the tools + tool_choice were bound onto the model, not executed
    assert harness.state["llm"].captured["tools"] == body["tools"]
    assert harness.state["llm"].captured["tool_choice"] == "auto"


def test_plain_reply_has_content_and_usage(harness):
    harness.state["response"] = _reply("just text", input_tokens=20, output_tokens=4)
    r = _post(harness, "prod-gateway", {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
    body = r.json()
    assert body["choices"][0]["message"]["content"] == "just text"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 20, "completion_tokens": 4, "total_tokens": 24}


# ── refusals (r3) ──────────────────────────────────────────────────────────────────────────────


def test_unknown_connection_404_names_registered(harness):
    r = _post(harness, "nope", {"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "prod-gateway" in detail and "local-vllm" in detail and "claude-sub" in detail


def test_stream_true_is_400(harness):
    r = _post(harness, "prod-gateway", {"model": "x", "messages": [{"role": "user", "content": "hi"}], "stream": True})
    assert r.status_code == 400
    # nothing was built for a rejected stream request
    assert harness.state["calls"] == []


def test_non_loopback_client_is_403(harness, monkeypatch):
    app = FastAPI()
    register_model_passthrough_routes(app)
    remote = TestClient(app, client=("10.1.2.3", 9999))
    r = remote.post(PATH.format(conn="prod-gateway"), json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 403
    assert harness.state["calls"] == []  # refused before any model build


def test_no_bearer_refused_by_api_auth():
    """Under /api, the default-deny operator bearer (a2a_impl.auth) gates it like every /api route."""
    from a2a_impl import auth

    cfg = LangGraphConfig.from_dict(DOC)
    app = FastAPI()
    register_model_passthrough_routes(app)
    app.add_middleware(auth.A2AAuthMiddleware)

    prev_bearer = auth._BEARER[0]
    prev_cfg = STATE.graph_config
    auth._BEARER[0] = "sekret"
    STATE.graph_config = cfg
    try:
        c = TestClient(app, client=("127.0.0.1", 5000))
        body = {"model": "x", "messages": [{"role": "user", "content": "hi"}]}
        # no Authorization header → refused before the handler ever runs
        assert c.post(PATH.format(conn="prod-gateway"), json=body).status_code == 401
        # with the bearer, auth lets it through to the handler (which 404s the unknown model)
        ok = c.post(PATH.format(conn="prod-gateway"), json=body, headers={"Authorization": "Bearer sekret"})
        assert ok.status_code != 401
    finally:
        auth._BEARER[0] = prev_bearer
        STATE.graph_config = prev_cfg


# ── usage recorded through the turn path, and no graph/agent run (r4) ───────────────────────────


def test_usage_recorded_and_no_graph_run(harness, monkeypatch):
    # A native subscription account resolves, so /api/usage can attribute the spend.
    monkeypatch.setattr("graph.delegation_usage.provider_account", lambda ptype: ("anthropic-oauth", "acct-xyz"))
    harness.state["response"] = _reply("done", input_tokens=30, output_tokens=6)

    r = _post(
        harness,
        "claude-sub",
        {"model": "claude-sonnet-4-6", "messages": [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, r.text

    # one telemetry row, billed to the subscription account, counted as one llm call
    assert len(harness.store.rows) == 1
    row = harness.store.rows[0]
    assert row["account_key"] == "acct-xyz"
    assert row["provider_id"] == "anthropic-oauth"
    assert row["model"] == "claude-sonnet-4-6"
    assert row["input_tokens"] == 30 and row["output_tokens"] == 6
    assert row["total_tokens"] == 36
    assert row["llm_calls"] == 1 and row["tool_calls"] == 0
    assert row["task_id"].startswith("model_passthrough:")

    # no graph/agent run: the model saw EXACTLY the converted messages — no SOUL system prompt,
    # no injected tools, no graph preprocessing.
    seen = harness.state["llm"].captured["messages"]
    assert [type(m) for m in seen] == [SystemMessage, HumanMessage]
    assert seen[0].content == "SYS" and seen[1].content == "hi"
