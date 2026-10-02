"""Every model call on an anthropic-oauth route carries the exact identity block.

Anthropic's OAuth enforcement refuses a request whose FIRST system block isn't
byte-exactly the Claude Code identity line with a fake 429 (``rate_limit_error``,
body "Error", no rate-limit headers, quota untouched — #2763, ADR 0097).
``ClaudeCodeIdentityMiddleware`` shapes the agent's MAIN model calls, but anything
that invokes the model directly — langchain's ``SummarizationMiddleware._create_summary``
calls ``self._summary_model.invoke("<prompt string>")`` with no system message at
all — never passes through it. With ``compaction.trigger`` set, the compaction call
got the fake 429 and killed the whole turn (``During task with name
'CountingSummarizationMiddleware.before_model'``).

These tests capture the outgoing request body at the httpx boundary (a
``MockTransport`` on the real SDK client), so they assert the wire, not an
intermediate object.
"""

from __future__ import annotations

import importlib
import json
import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from graph.config import LangGraphConfig

IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."


def _sse_body(text: str = "a summary") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _http_mod(llm):
    """The httpx flavour the SDK's base client imports (anthropic>=1.8 uses ``httpx2``)."""
    import anthropic._base_client as base

    return getattr(base, "httpx2", None) or importlib.import_module("httpx")


def _json_body(text: str = "a summary") -> dict:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 3},
    }


class _Capture:
    """httpx transport handler that records each request body and answers like Anthropic."""

    def __init__(self, *, fail: bool = False):
        self.bodies: list[dict] = []
        self.fail = fail

    httpx = None  # set by _wire to the SDK's own httpx module

    def __call__(self, request):
        httpx = self.httpx
        body = json.loads(request.read() or b"{}")
        self.bodies.append(body)
        if self.fail:
            # The exact fake-429 shape the OAuth enforcement returns (no ratelimit headers).
            return httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "Error"}})
        if body.get("stream"):
            return httpx.Response(200, content=_sse_body(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=_json_body())


def _wire(llm, capture: _Capture) -> None:
    """Swap the real SDK clients' httpx transport for the capturing mock."""
    httpx = capture.httpx = _http_mod(llm)
    llm._client._client = httpx.Client(transport=httpx.MockTransport(capture))
    llm._async_client._client = httpx.AsyncClient(transport=httpx.MockTransport(capture))


@pytest.fixture
def oauth_llm(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "cc-WIRE")
    import graph.providers.anthropic_oauth as ao

    if ao._OAuthChatAnthropic is None:  # pragma: no cover — langchain-anthropic absent
        pytest.skip("langchain-anthropic not installed")
    ao._reset_token_cache()
    from graph.llm import create_llm

    llm = create_llm(LangGraphConfig(model_provider="anthropic-oauth", model_name="claude-opus-5-5"))
    # No retries: the failing-path test must see exactly one request.
    llm.max_retries = 0
    yield llm
    ao._reset_token_cache()


def _summarizer(model):
    from graph.middleware.compaction import CountingSummarizationMiddleware

    return CountingSummarizationMiddleware(model=model, trigger=("messages", 4), keep=("messages", 2))


def _long_state() -> dict:
    msgs = []
    for i in range(6):
        msgs.append(HumanMessage(f"question {i}", id=f"h{i}"))
        msgs.append(AIMessage(f"answer {i}", id=f"a{i}"))
    return {"messages": msgs, "session_id": "s-test", "incognito": True}


def _first_system_block(body: dict):
    system = body.get("system")
    assert isinstance(system, list) and system, f"system must be a block list, got {system!r}"
    return system[0]


# ── the proof: the compaction call's wire body ────────────────────────────────


def test_summarization_request_first_system_block_is_exact_identity(oauth_llm):
    """The live bug: SummarizationMiddleware invokes the model with a bare string, so
    the request had NO system field at all → fake 429. Now the client itself shapes it."""
    cap = _Capture()
    _wire(oauth_llm, cap)
    out = _summarizer(oauth_llm).before_model(_long_state(), None)
    assert out is not None, "compaction should have fired"
    assert len(cap.bodies) == 1
    assert _first_system_block(cap.bodies[0]) == {"type": "text", "text": IDENTITY}


@pytest.mark.asyncio
async def test_summarization_request_async_path_identity(oauth_llm):
    cap = _Capture()
    _wire(oauth_llm, cap)
    out = await _summarizer(oauth_llm).abefore_model(_long_state(), None)
    assert out is not None
    assert len(cap.bodies) == 1
    assert _first_system_block(cap.bodies[0]) == {"type": "text", "text": IDENTITY}


# ── the client-level shaping covers every direct call shape ───────────────────


@pytest.mark.parametrize(
    "messages, expect_rest",
    [
        ([HumanMessage("hi")], []),  # no system at all (titles, distill, summarization)
        ([SystemMessage("You are Aria."), HumanMessage("hi")], ["You are Aria."]),  # plain persona
        ([SystemMessage(f"{IDENTITY}\n\nYou are Aria."), HumanMessage("hi")], ["You are Aria."]),  # merged → split
        ([SystemMessage(IDENTITY), HumanMessage("hi")], []),  # already exact (the probe shape)
    ],
)
def test_direct_invoke_shapes_identity(oauth_llm, messages, expect_rest):
    cap = _Capture()
    _wire(oauth_llm, cap)
    oauth_llm.invoke(messages)
    system = cap.bodies[0]["system"]
    assert system[0] == {"type": "text", "text": IDENTITY}
    assert [b["text"] for b in system[1:]] == expect_rest
    # Never stacked.
    assert sum(b["text"].count(IDENTITY) for b in system) == 1


def test_identity_block_preserves_cache_control_on_the_remainder(oauth_llm):
    cap = _Capture()
    _wire(oauth_llm, cap)
    persona = {"type": "text", "text": f"{IDENTITY}\n\nYou are Aria.", "cache_control": {"type": "ephemeral"}}
    oauth_llm.invoke([SystemMessage(content=[persona]), HumanMessage("hi")])
    system = cap.bodies[0]["system"]
    assert system[0] == {"type": "text", "text": IDENTITY}  # exact, no extra keys
    assert system[1] == {"type": "text", "text": "You are Aria.", "cache_control": {"type": "ephemeral"}}


def test_non_oauth_anthropic_route_unchanged(monkeypatch):
    """A plain (API-key) ChatAnthropic must never get the identity line."""
    from langchain_anthropic import ChatAnthropic

    llm = ChatAnthropic(model="claude-opus-5-5", api_key="sk-test", max_retries=0)
    cap = _Capture()
    _wire(llm, cap)
    llm.invoke([HumanMessage("hi")])
    assert "system" not in cap.bodies[0]
    llm.invoke([SystemMessage("You are Aria."), HumanMessage("hi")])
    assert cap.bodies[1]["system"] == "You are Aria."


# ── a failing compaction never fails the turn ─────────────────────────────────


def test_failing_summarization_does_not_fail_the_turn(oauth_llm, caplog):
    cap = _Capture(fail=True)
    _wire(oauth_llm, cap)
    with caplog.at_level(logging.WARNING, logger="graph.middleware.compaction"):
        out = _summarizer(oauth_llm).before_model(_long_state(), None)
    assert out is None  # no compaction this turn — history untouched, turn continues
    assert cap.bodies, "the summary call was attempted"
    assert any("compaction" in r.getMessage() and "FAILED" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_failing_summarization_async_does_not_fail_the_turn(oauth_llm, caplog):
    cap = _Capture(fail=True)
    _wire(oauth_llm, cap)
    with caplog.at_level(logging.WARNING, logger="graph.middleware.compaction"):
        out = await _summarizer(oauth_llm).abefore_model(_long_state(), None)
    assert out is None
    assert any("FAILED" in r.getMessage() for r in caplog.records)


def test_graph_control_flow_still_propagates_from_compaction(monkeypatch):
    """Only real failures are swallowed — an interrupt is graph control flow, not an error."""
    from langchain.agents.middleware import SummarizationMiddleware
    from langgraph.errors import GraphInterrupt

    from graph.middleware.compaction import CountingSummarizationMiddleware

    def _boom(self, state, runtime):
        raise GraphInterrupt(())

    monkeypatch.setattr(SummarizationMiddleware, "before_model", _boom)
    with pytest.raises(GraphInterrupt):
        object.__new__(CountingSummarizationMiddleware).before_model({"messages": []}, None)
