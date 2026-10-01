"""#3957 — API error shapes.

1. ``/api/subagents/run`` (and ``/batch``) turned a provider 429 into HTTP 500. It now
   mirrors 429 (with the provider's ``Retry-After``) and maps any other upstream failure,
   or an unreachable gateway, to 502 — the same policy ``/v1`` uses.
2. A per-turn model override that cannot be built (``/v1``'s ``model``, a console tab's
   pick, an A2A ``metadata.model``) silently ran the turn on the DEFAULT model ("could
   not switch … using default"). It now fails the turn with a short error naming the pick,
   on every surface: an unknown connection prefix is the caller's error (``/v1`` 400), a
   known connection that could not be built right now — a sign-in refresh — is not
   (``/v1`` 503).
3. Upstream classification follows only the explicit ``__cause__`` chain.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from graph.middleware.model_override import ModelOverrideError, ModelOverrideMiddleware, ModelUnavailableError

# ── 1. /api/subagents/run|batch ────────────────────────────────────────────────────


class _RateLimited(Exception):
    """Shaped like the openai SDK's RateLimitError: a status and a response."""

    status_code = 429

    def __init__(self):
        super().__init__("Error code: 429 - usage_limit_reached")
        self.response = httpx.Response(429, headers={"retry-after": "17"}, request=httpx.Request("POST", "http://x"))


class _BadGateway(Exception):
    status_code = 503


def _wrapped(exc: BaseException) -> BaseException:
    """What the delegation funnel raises: ``SubagentError(...) from e``."""
    from graph.agent import SubagentError

    try:
        raise SubagentError(f"Subagent 'researcher' failed: {exc}") from exc
    except SubagentError as wrapped:
        return wrapped


def _routes_client(run_exc: BaseException | None = None, batch_exc: BaseException | None = None) -> TestClient:
    from operator_api.routes import register_operator_routes

    async def _run(_req):
        raise run_exc

    async def _batch(_req):
        raise batch_exc

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_run,
        subagent_batch=_batch,
    )
    return TestClient(app, raise_server_exceptions=False)


def test_subagent_run_upstream_429_is_429_with_retry_after():
    c = _routes_client(run_exc=_wrapped(_RateLimited()))

    r = c.post("/api/subagents/run", json={"prompt": "x", "type": "researcher"})

    assert r.status_code == 429  # was 500
    assert r.headers.get("retry-after") == "17"
    assert "rate-limited" in r.json()["detail"] and "429" in r.json()["detail"]


def test_subagent_batch_upstream_429_is_429():
    c = _routes_client(batch_exc=_wrapped(_RateLimited()))

    r = c.post("/api/subagents/batch", json={"tasks": [{"prompt": "x"}]})

    assert r.status_code == 429


@pytest.mark.parametrize(
    "exc",
    [
        _wrapped(_BadGateway("upstream down")),
        _wrapped(httpx.ConnectError("connection refused")),
    ],
    ids=["upstream-5xx", "unreachable"],
)
def test_subagent_run_other_upstream_failures_are_502(exc):
    r = _routes_client(run_exc=exc).post("/api/subagents/run", json={"prompt": "x"})

    assert r.status_code == 502


@pytest.mark.parametrize(
    "exc, status",
    [(ValueError("unknown subagent"), 400), (RuntimeError("agent graph is not loaded"), 409), (KeyError("bug"), 500)],
)
def test_subagent_run_non_upstream_errors_keep_their_status(exc, status):
    r = _routes_client(run_exc=exc).post("/api/subagents/run", json={"prompt": "x"})

    assert r.status_code == status


# ── 2. an unbuildable model override fails explicitly ──────────────────────────────


class _Req:
    def __init__(self, state):
        self.state = state
        self.model = type("M", (), {"model_name": "default-model"})()
        self.overridden = None

    def override(self, **kw):
        self.overridden = kw
        return self


def _missing_credentials(*_a, **_k):
    raise RuntimeError("Missing credentials. Please pass an `api_key`")


def test_an_unknown_connection_prefix_raises_a_short_caller_error(monkeypatch):
    monkeypatch.setattr("graph.llm.create_llm", _missing_credentials)
    mw = ModelOverrideMiddleware(LangGraphConfig())

    with pytest.raises(ModelOverrideError) as ei:
        mw._override(_Req({"model": "nonexistent-provider:bogus-model-xyz"}))

    msg = str(ei.value)
    assert ei.value.model == "nonexistent-provider:bogus-model-xyz"
    assert msg == "model 'nonexistent-provider:bogus-model-xyz' is not available: 'nonexistent-provider' is not a known connection."
    # Neither the build error nor the agent's connection list reaches the caller.
    assert "Missing credentials" not in msg and "anthropic-oauth" not in msg and "gateway" not in msg


def test_a_known_connection_that_cannot_build_is_unavailable_not_a_caller_error(monkeypatch):
    """A sign-in refresh failing (often transient) is not the caller's fault: not a 400."""

    def _refresh_failed(*_a, **_k):
        raise RuntimeError("token refresh failed: 503 from auth server; secret=abc123")

    monkeypatch.setattr("graph.llm.create_llm", _refresh_failed)
    mw = ModelOverrideMiddleware(LangGraphConfig())

    with pytest.raises(ModelUnavailableError) as ei:
        mw._override(_Req({"model": "anthropic-oauth:claude-sonnet-4-6"}))

    assert not isinstance(ei.value, ValueError)
    assert "secret" not in str(ei.value) and "refresh" not in str(ei.value)
    assert "could not be loaded right now" in str(ei.value)


def test_an_effort_only_failure_still_degrades_to_the_current_model(monkeypatch):
    monkeypatch.setattr("graph.llm.create_llm", _missing_credentials)
    mw = ModelOverrideMiddleware(LangGraphConfig())
    req = _Req({"reasoning_effort": "high"})

    assert mw._override(req) is req and req.overridden is None


def _rewrapped(inner: BaseException) -> BaseException:
    try:
        try:
            raise inner
        except type(inner) as e:
            raise RuntimeError("graph node failed") from e  # a framework re-wrap
    except RuntimeError as outer:
        return outer


def test_turn_error_classifies_pick_failures():
    import importlib

    chat_mod = importlib.import_module("server.chat")

    bad = chat_mod.turn_error(_rewrapped(ModelOverrideError("nope:x", "nope")))
    assert bad["type"] == "invalid_request_error" and bad["param"] == "model"
    assert bad["message"] == "model 'nope:x' is not available: 'nope' is not a known connection."

    down = chat_mod.turn_error(_rewrapped(ModelUnavailableError("anthropic-oauth:m")))
    assert down["type"] == "server_error" and down["model_unavailable"] is True


def _v1_client(monkeypatch, exc):
    import importlib

    import operator_api.chat_routes as cr
    import runtime.state as rs

    chat_mod = importlib.import_module("server.chat")

    async def _fake_chat(message, session_id, **_kw):
        msg = str(exc)
        return [{"role": "assistant", "content": f"**Error:** {msg}", "error": chat_mod.turn_error(exc, msg)}]

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def test_v1_answers_an_unknown_connection_400(monkeypatch):
    c = _v1_client(monkeypatch, ModelOverrideError("nonexistent-provider:bogus", "nonexistent-provider"))

    r = c.post("/v1/chat/completions", json={"model": "nonexistent-provider:bogus", "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 400  # was 200 on the default model
    body = r.json()["error"]
    assert body["type"] == "invalid_request_error" and body["param"] == "model"
    assert "not a known connection" in body["message"]


def test_v1_answers_an_unbuildable_known_connection_503(monkeypatch):
    c = _v1_client(monkeypatch, ModelUnavailableError("anthropic-oauth:claude-sonnet-4-6"))

    r = c.post("/v1/chat/completions", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})

    assert r.status_code == 503


# ── 3. only the explicit cause chain is upstream evidence ──────────────────────────


def test_an_own_bug_raised_while_handling_a_transport_error_is_not_upstream():
    from graph.upstream_errors import upstream_http_status

    try:
        try:
            raise httpx.ConnectError("refused")
        except httpx.ConnectError:
            {}["missing"]  # our own bug, raised inside the except: __context__ only
    except KeyError as bug:
        assert bug.__context__ is not None and bug.__cause__ is None
        assert upstream_http_status(bug) is None  # a 500, not a mislabelled 502

    try:
        try:
            raise _RateLimited()
        except _RateLimited:
            raise ValueError("bad input")
    except ValueError as bug:
        assert upstream_http_status(bug) is None

    assert upstream_http_status(_wrapped(httpx.ConnectError("refused"))) == 502  # `from e`: followed


def test_v1_upstream_statuses_are_unchanged():
    from operator_api.chat_routes import _v1_error_response

    assert _v1_error_response({"upstream_status": 429}).status_code == 429
    assert _v1_error_response({"upstream_status": 400, "type": "invalid_request_error"}).status_code == 502
    assert _v1_error_response({"upstream_status": None, "upstream_unreachable": True}).status_code == 502
    assert _v1_error_response({"upstream_status": None, "type": "server_error"}).status_code == 500
