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
from observability import tracing

_TID = "a" * 32
_SID = "b" * 16


class _FakeModel:
    """Pydantic-shaped stand-in: has an extra_body slot + model_copy(update=...)."""

    def __init__(self, extra_body=None):
        self.extra_body = extra_body

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
    model = ChatOpenAI(api_key="x", model="gw/model", extra_body={"top_k": 20})
    out = mw._with_trace(_FakeRequest(model))

    payload = out.model._get_request_payload([("human", "hi")])
    meta = payload["extra_body"]["metadata"]
    assert meta["existing_trace_id"] == _TID
    assert meta["parent_observation_id"] == _SID
    assert payload["extra_body"]["top_k"] == 20
    # the shared original stays clean
    assert model.extra_body == {"top_k": 20}


async def test_awrap_model_call_passes_stamped_request_to_handler(monkeypatch, mw):
    _set_ctx(monkeypatch, {"trace_id": _TID})
    seen = {}

    async def handler(request):
        seen["req"] = request
        return "resp"

    assert await mw.awrap_model_call(_FakeRequest(_FakeModel()), handler) == "resp"
    assert seen["req"].model.extra_body["metadata"]["existing_trace_id"] == _TID


# ─── Fleet generation node (whole-trace in the agent's OWN project) ──────────
# The gateway logs the full-detail generation into ITS project; when the agent
# runs in a different (fleet) project, the middleware also emits a lightweight
# model+usage+cost generation into the agent's project so its trace is whole.


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

    mw._emit_fleet_generation(_ai(usage={"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}), 0)

    assert calls and calls[0]["model"] == "gw/model"


def test_emit_noop_when_tracing_disabled(monkeypatch, mw):
    monkeypatch.setattr(tracing, "is_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(tracing, "trace_generation", lambda **kw: calls.append(kw))

    mw._emit_fleet_generation(
        _Resp([_ai(usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]), 0
    )

    assert not calls


def test_emit_never_raises_on_garbage_or_sink_blowup(monkeypatch, mw):
    monkeypatch.setattr(tracing, "is_enabled", lambda: True)

    def _boom(**kw):
        raise RuntimeError("otel misery")

    monkeypatch.setattr(tracing, "trace_generation", _boom)
    # garbage response (no .result / no usage) → nothing to emit, no raise
    mw._emit_fleet_generation(object(), 0)
    # valid response but the sink throws → still swallowed
    mw._emit_fleet_generation(
        _Resp([_ai(usage={"input_tokens": 1, "output_tokens": 1, "total_tokens": 2})]), 0
    )


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
