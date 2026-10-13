"""TraceContextMiddleware — per-call Langfuse trace join for gateway LLM calls.

The LiteLLM gateway's own Langfuse callback honors request-body ``metadata``
keys ``existing_trace_id`` / ``parent_observation_id`` / ``generation_name``
(verified against litellm 1.83.10). This middleware stamps them onto a copy of
the request's model via ``extra_body`` at the ``wrap_model_call`` boundary —
fresh ids per call, no-op when tracing is inactive, never raises.
"""

from __future__ import annotations

import pytest

from graph.middleware.trace_context import TraceContextMiddleware
from graph.providers.identity import tag_model_provider
from observability import tracing

_TID = "a" * 32
_SID = "b" * 16


class _FakeModel:
    """Pydantic-shaped stand-in: has an extra_body slot + model_copy(update=...).

    Tagged as a gateway (openai-compat) client, the way ``create_llm`` tags every
    gateway build — only those get the stamp (#3928)."""

    def __init__(self, extra_body=None, provider_type="openai-compat"):
        self.extra_body = extra_body
        tag_model_provider(self, provider_type, "gateway" if provider_type == "openai-compat" else provider_type)

    def model_copy(self, update=None):
        clone = _FakeModel(self.extra_body)
        for k, v in (update or {}).items():
            setattr(clone, k, v)
        return clone


class _FakeRequest:
    def __init__(self, model):
        self.model = model

    def override(self, model=None):
        return _FakeRequest(model)


@pytest.fixture
def mw():
    return TraceContextMiddleware()


def _set_ctx(monkeypatch, ctx):
    monkeypatch.setattr(tracing, "current_trace_context", lambda: ctx)


def test_stamps_trace_metadata_onto_model_copy(monkeypatch, mw):
    _set_ctx(monkeypatch, {"trace_id": _TID, "span_id": _SID})
    monkeypatch.setenv("AGENT_NAME", "vera")
    req = _FakeRequest(_FakeModel())

    out = mw._with_trace(req)

    assert out is not req  # overridden
    meta = out.model.extra_body["metadata"]
    assert meta["existing_trace_id"] == _TID
    assert meta["parent_observation_id"] == _SID
    assert meta["generation_name"] == "vera-turn"


def test_trace_id_only_omits_parent_observation(monkeypatch, mw):
    _set_ctx(monkeypatch, {"trace_id": _TID})
    out = mw._with_trace(_FakeRequest(_FakeModel()))
    meta = out.model.extra_body["metadata"]
    assert meta["existing_trace_id"] == _TID
    assert "parent_observation_id" not in meta


def test_merges_with_existing_extra_body_without_mutating_original(monkeypatch, mw):
    """A gateway model may already carry extra_body (top_k, thinking, …) — the
    stamp must merge, and the ORIGINAL model must stay untouched (it's shared
    across turns via the compiled graph / middleware caches)."""
    _set_ctx(monkeypatch, {"trace_id": _TID})
    original = _FakeModel({"top_k": 20, "metadata": {"custom": "keep"}})
    out = mw._with_trace(_FakeRequest(original))

    assert out.model.extra_body["top_k"] == 20
    assert out.model.extra_body["metadata"]["custom"] == "keep"
    assert out.model.extra_body["metadata"]["existing_trace_id"] == _TID
    # original untouched
    assert original.extra_body == {"top_k": 20, "metadata": {"custom": "keep"}}


def test_noop_when_tracing_inactive(monkeypatch, mw):
    _set_ctx(monkeypatch, None)
    req = _FakeRequest(_FakeModel())
    assert mw._with_trace(req) is req


def test_noop_for_models_without_extra_body(monkeypatch, mw):
    """ACP aux models (and fakes) have no extra_body slot — leave them alone."""
    _set_ctx(monkeypatch, {"trace_id": _TID})
    req = _FakeRequest(object())
    assert mw._with_trace(req) is req


def test_tracing_blowup_never_breaks_the_model_call(monkeypatch, mw):
    def _boom():
        raise RuntimeError("otel misery")

    monkeypatch.setattr(tracing, "current_trace_context", _boom)
    req = _FakeRequest(_FakeModel())
    assert mw._with_trace(req) is req


def test_real_chatopenai_payload_carries_the_stamp(monkeypatch, mw):
    """End-to-end through the REAL client class: the stamped copy's request
    payload includes extra_body.metadata — i.e. the gateway will actually see
    existing_trace_id on the wire."""
    from langchain_openai import ChatOpenAI

    _set_ctx(monkeypatch, {"trace_id": _TID, "span_id": _SID})
    model = tag_model_provider(
        ChatOpenAI(api_key="x", model="gw/model", extra_body={"top_k": 20}), "openai-compat", "gateway"
    )
    out = mw._with_trace(_FakeRequest(model))

    payload = out.model._get_request_payload([("human", "hi")])
    meta = payload["extra_body"]["metadata"]
    assert meta["existing_trace_id"] == _TID
    assert meta["parent_observation_id"] == _SID
    assert payload["extra_body"]["top_k"] == 20
    # the shared original stays clean
    assert model.extra_body == {"top_k": 20}


# ─── Gateway lane only (#3928) ────────────────────────────────────────────────
# The native Codex client is a ChatOpenAI with an ``extra_body`` slot too; stamping
# it sent ``metadata`` to chatgpt.com/backend-api/codex/responses, which 400s
# "Unsupported parameter: metadata" — every turn on a native Codex model failed.


@pytest.mark.parametrize("provider_type", ["openai-codex", "anthropic-oauth", "acp", ""])
def test_non_gateway_models_are_never_stamped(monkeypatch, mw, provider_type):
    _set_ctx(monkeypatch, {"trace_id": _TID, "span_id": _SID})
    req = _FakeRequest(_FakeModel({"top_k": 1}, provider_type=provider_type))
    out = mw._with_trace(req)
    assert out is req
    assert "metadata" not in (out.model.extra_body or {})


_CODEX_BASE = "https://chatgpt.example/backend-api/codex"
_GATEWAY_BASE = "https://gw.example.com/v1"


def _capture_wire(monkeypatch) -> list[dict]:
    """Capture the request the OpenAI SDK actually builds — after it has merged
    ``extra_body`` into the JSON body — and stop it there, so no network is touched.

    Hooked at the SDK's ``_build_request`` rather than an httpx transport: the SDK
    ships its own HTTP stack, and this is the last point where the body is final
    whatever that stack is."""
    import json

    from openai._base_client import SyncAPIClient

    sent: list[dict] = []
    real_build = SyncAPIClient._build_request

    def _build(self, options, *args, **kwargs):
        request = real_build(self, options, *args, **kwargs)
        if request.method == "POST":  # skip the gateway's GET /model/info probes
            sent.append({"url": str(request.url), "body": json.loads(request.read() or b"{}")})
            raise RuntimeError("captured")
        return request

    monkeypatch.setattr(SyncAPIClient, "_build_request", _build)
    return sent


def _invoke_through_middleware(mw, model) -> None:
    """Run one model call through ``wrap_model_call`` the way the agent does."""

    def handler(request):
        try:
            return request.model.invoke("hi")
        except Exception:  # noqa: BLE001 — the capture stops the call; the payload is the point
            return None

    mw.wrap_model_call(_FakeRequest(model), handler)


def test_native_codex_request_on_the_wire_carries_no_metadata(monkeypatch, mw):
    """The client ``create_llm`` builds for ``openai-codex`` — through the real
    Responses payload builder and the real SDK — sends no ``metadata`` even while a
    Langfuse trace is active."""
    import graph.providers.openai_codex as ocx
    from graph.config import LangGraphConfig
    from graph.llm import create_llm
    from graph.providers.oauth import CodexOAuthCreds

    monkeypatch.setattr(
        ocx,
        "resolve_codex_oauth",
        lambda *a, **k: CodexOAuthCreds(access_token="t", account_id="a", base_url=_CODEX_BASE, source="s"),
    )
    monkeypatch.setattr(tracing, "is_enabled", lambda: False)  # keep the emit side quiet
    _set_ctx(monkeypatch, {"trace_id": _TID, "span_id": _SID})
    sent = _capture_wire(monkeypatch)

    llm = create_llm(LangGraphConfig(model_provider="openai-codex", model_name="gpt-5.6-sol", llm_max_retries=0))
    _invoke_through_middleware(mw, llm)

    assert sent, "the client never reached the wire"
    assert sent[0]["url"].startswith(_CODEX_BASE + "/responses")
    body = sent[0]["body"]
    assert "metadata" not in body
    assert _TID not in str(body)


def test_gateway_request_on_the_wire_still_carries_the_trace(monkeypatch, mw):
    from graph.config import LangGraphConfig
    from graph.llm import create_llm

    monkeypatch.setattr(tracing, "is_enabled", lambda: False)
    _set_ctx(monkeypatch, {"trace_id": _TID, "span_id": _SID})
    sent = _capture_wire(monkeypatch)

    llm = create_llm(
        LangGraphConfig(
            model_provider="",
            model_name="protolabs/reasoning",
            api_base=_GATEWAY_BASE,
            api_key="gw-key",
            llm_max_retries=0,
        )
    )
    _invoke_through_middleware(mw, llm)

    assert sent and sent[0]["url"].startswith(_GATEWAY_BASE)
    meta = sent[0]["body"]["metadata"]
    assert meta["existing_trace_id"] == _TID
    assert meta["parent_observation_id"] == _SID


def test_native_anthropic_oauth_model_is_left_untouched(monkeypatch, mw):
    from graph.config import LangGraphConfig
    from graph.llm import create_llm

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-X")
    _set_ctx(monkeypatch, {"trace_id": _TID, "span_id": _SID})
    llm = create_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-5"))
    req = _FakeRequest(llm)
    out = mw._with_trace(req)
    assert out is req and out.model is llm
    assert "metadata" not in (getattr(llm, "model_kwargs", None) or {})


async def test_awrap_model_call_passes_stamped_request_to_handler(monkeypatch, mw):
    _set_ctx(monkeypatch, {"trace_id": _TID})
    seen = {}

    async def handler(request):
        seen["req"] = request
        return "resp"

    assert await mw.awrap_model_call(_FakeRequest(_FakeModel()), handler) == "resp"
    assert seen["req"].model.extra_body["metadata"]["existing_trace_id"] == _TID


# ─── Fleet generation node (whole-trace in the agent's OWN project) ──────────
# The gateway logs the full-detail generation into ITS project; the middleware
# also emits the generation (model + usage + cost + capped IO) into the agent's
# own project, so its trace is whole even when the call never touched the gateway.


class _Resp:
    """ModelResponse stand-in: carries .result (list[BaseMessage])."""

    def __init__(self, result):
        self.result = result


def _ai(usage=None, model="gw/model"):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="ok",
        usage_metadata=usage,
        response_metadata=({"model_name": model} if model else {}),
    )


def test_emit_fleet_generation_records_model_usage_cost(monkeypatch, mw):
    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    monkeypatch.setenv("AGENT_NAME", "vera")
    seen = {}
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: seen.update(kw))

    mw._emit_fleet_generation(
        None,
        _Resp([_ai(usage={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})]),
        1234,
    )

    assert seen["name"] == "vera-turn"
    assert seen["model"] == "gw/model"
    assert seen["usage"]["input_tokens"] == 100
    assert seen["duration_ms"] == 1234
    assert seen["cost_usd"] >= 0.0


def test_emit_handles_bare_aimessage_response(monkeypatch, mw):
    """The contract allows the handler to return an AIMessage directly."""
    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: calls.append(kw))

    mw._emit_fleet_generation(None, _ai(usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}), 0)

    assert calls and calls[0]["model"] == "gw/model"


def test_emit_noop_when_tracing_disabled(monkeypatch, mw):
    monkeypatch.setattr(tracing, "is_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: calls.append(kw))

    mw._emit_fleet_generation(None, _Resp([_ai(usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]), 0)

    assert not calls


def test_emit_never_raises_on_garbage_or_sink_blowup(monkeypatch, mw):
    monkeypatch.setattr(tracing, "is_enabled", lambda: True)

    def _boom(**kw):
        raise RuntimeError("otel misery")

    monkeypatch.setattr(tracing, "trace_generation", _boom)
    # garbage response (no .result / no usage) → nothing to emit, no raise
    mw._emit_fleet_generation(None, object(), 0)
    # valid response but the sink throws → still swallowed
    mw._emit_fleet_generation(None, _Resp([_ai(usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]), 0)


async def test_awrap_emits_generation_and_returns_response(monkeypatch, mw):
    _set_ctx(monkeypatch, {"trace_id": _TID})
    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    calls = []
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: calls.append(kw))
    resp = _Resp([_ai(usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12})])

    async def handler(request):
        return resp

    out = await mw.awrap_model_call(_FakeRequest(_FakeModel()), handler)

    assert out is resp  # response passes through untouched
    assert calls and calls[0]["usage"]["output_tokens"] == 2


# ─── Generation IO + the turn's answer ────────────────────────────────────────


class _IORequest:
    def __init__(self, system, messages):
        self.system_message = system
        self.messages = messages


def test_generation_carries_the_calls_messages_and_reply(monkeypatch, mw):
    """Without IO an agent on a native OAuth provider (no gateway) logged its
    prompts and replies NOWHERE — every generation showed blank input/output."""
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    seen = {}
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: seen.update(kw))
    monkeypatch.setattr(tracing, "set_session_output", lambda out: seen.setdefault("answer", out))
    req = _IORequest(
        SystemMessage(content="you are the PM"),
        [
            HumanMessage(content="status?"),
            AIMessage(content="", tool_calls=[{"name": "board_list", "args": {"q": "open"}, "id": "c1"}]),
            ToolMessage(content="3 open", tool_call_id="c1", name="board_list"),
        ],
    )

    mw._emit_fleet_generation(req, _Resp([AIMessage(content=[{"type": "text", "text": "Three open."}])]), 0)

    assert [m["role"] for m in seen["input"]] == ["system", "user", "assistant", "tool"]
    assert seen["input"][0]["content"] == "you are the PM"
    assert seen["input"][2]["tool_calls"] == [{"name": "board_list", "args": "{'q': 'open'}"}]
    assert seen["input"][3] == {"role": "tool", "content": "3 open", "name": "board_list"}
    assert seen["output"] == {"role": "assistant", "content": "Three open."}
    # A tool-call-free reply is the turn's answer.
    assert seen["answer"] == "Three open."


def test_a_tool_call_reply_is_not_the_turns_answer(monkeypatch, mw):
    from langchain_core.messages import AIMessage

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: None)
    answers = []
    monkeypatch.setattr(tracing, "set_session_output", answers.append)

    reply = AIMessage(content="checking", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])
    mw._emit_fleet_generation(None, _Resp([reply]), 0)

    assert answers == []


def test_io_is_capped_per_message(monkeypatch, mw):
    from langchain_core.messages import AIMessage, SystemMessage

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    monkeypatch.setattr(tracing, "MAX_IO_CHARS", 10)
    seen = {}
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: seen.update(kw))
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)

    mw._emit_fleet_generation(_IORequest(SystemMessage(content="x" * 25), []), _Resp([AIMessage(content="ok")]), 0)

    assert seen["input"][0]["content"] == "x" * 10 + "… [15 more chars]"


def test_incognito_call_records_usage_but_no_content(monkeypatch, mw):
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    seen = {}
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: seen.update(kw))
    answers = []
    monkeypatch.setattr(tracing, "set_session_output", answers.append)
    req = _IORequest(None, [HumanMessage(content="my SSN is 123-45-6789")])
    req.state = {"incognito": True}

    mw._emit_fleet_generation(
        req,
        _Resp([AIMessage(content="noted", usage_metadata={"input_tokens": 3, "output_tokens": 1, "total_tokens": 4})]),
        0,
    )

    assert seen["usage"]["input_tokens"] == 3
    assert "input" not in seen and "output" not in seen
    assert answers == []


def test_io_is_redacted(monkeypatch, mw):
    from langchain_core.messages import AIMessage, HumanMessage

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    seen = {}
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: seen.update(kw))
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)
    secret = "sk-" + "A" * 40
    req = _IORequest(None, [HumanMessage(content=f"use {secret}")])
    reply = AIMessage(content="", tool_calls=[{"name": "call_api", "args": {"api_key": secret}, "id": "c1"}])

    mw._emit_fleet_generation(req, _Resp([reply]), 0)

    assert secret not in str(seen["input"]) and secret not in str(seen["output"])


def test_history_beyond_the_call_budget_is_counted_not_sent(monkeypatch, mw):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    monkeypatch.setattr(tracing, "is_enabled", lambda: True)
    monkeypatch.setattr(tracing, "MAX_IO_CALL_CHARS", 250)
    seen = {}
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: seen.update(kw))
    monkeypatch.setattr(tracing, "set_session_output", lambda out: None)
    history = [HumanMessage(content=f"m{i:02d}" + "x" * 47) for i in range(10)]  # 50 chars each

    mw._emit_fleet_generation(_IORequest(SystemMessage(content="s" * 50), history), _Resp([AIMessage(content="ok")]), 0)

    sent = seen["input"]
    assert sent[0]["content"] == "s" * 50
    assert sent[1] == {"role": "system", "content": "[6 earlier messages omitted]"}
    assert [m["content"][:3] for m in sent[2:]] == ["m06", "m07", "m08", "m09"]
