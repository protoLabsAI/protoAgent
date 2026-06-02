"""Tests for the structured-skill finalizer (ADR-0006 addendum / #476).

The forced-tool-call loop is exercised with a mocked LLM (the real
``protolabs_a2a`` helpers do the tool-spec + validate + DataPart). The metadata
dispatch (request-level skillHint, the latent bug fix) is covered separately.
"""

from __future__ import annotations

import pytest

import protolabs_a2a as pa
from a2a_executor import _extract_caller_trace, _extract_skill_hint, _request_metadata
from graph.structured_skill import finalize_structured

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}},
    "required": ["verdict"],
}
_MIME = "application/vnd.protolabs.market-review-v1+json"


class _Bound:
    """A bound LLM that replays a queue of tool-call args (None ⇒ no tool call)."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    async def ainvoke(self, msgs):
        self.calls += 1
        args = self._replies.pop(0)
        tc = [] if args is None else [{"name": "submit_x", "args": args, "id": "1"}]
        return type("Resp", (), {"tool_calls": tc})()


class _LLM:
    def __init__(self, bound):
        self._bound = bound

    def bind_tools(self, tools, tool_choice=None):
        return self._bound


def _patch_llm(monkeypatch, bound):
    monkeypatch.setattr("graph.llm.create_llm", lambda cfg, **k: _LLM(bound))


@pytest.mark.asyncio
async def test_valid_first_call_emits_datapart(monkeypatch):
    bound = _Bound([{"verdict": "reject"}])
    _patch_llm(monkeypatch, bound)
    part = await finalize_structured("market_review", _SCHEMA, _MIME, "…reject…", config=None)
    assert bound.calls == 1
    assert pa.parse_skill_result(part, _MIME) == {"verdict": "reject"}  # roundtrips by MIME


@pytest.mark.asyncio
async def test_invalid_then_repaired(monkeypatch):
    bound = _Bound([{}, {"verdict": "approve"}])  # missing required → repair → valid
    _patch_llm(monkeypatch, bound)
    part = await finalize_structured("market_review", _SCHEMA, _MIME, "…", config=None)
    assert bound.calls == 2
    assert pa.parse_skill_result(part, _MIME) == {"verdict": "approve"}


@pytest.mark.asyncio
async def test_invalid_twice_degrades_to_none(monkeypatch):
    bound = _Bound([{}, {}])  # invalid both times → text-only
    _patch_llm(monkeypatch, bound)
    assert await finalize_structured("market_review", _SCHEMA, _MIME, "…", config=None) is None
    assert bound.calls == 2


@pytest.mark.asyncio
async def test_no_tool_call_degrades_to_none(monkeypatch):
    bound = _Bound([None])  # model didn't call the tool
    _patch_llm(monkeypatch, bound)
    assert await finalize_structured("market_review", _SCHEMA, _MIME, "…", config=None) is None


# ── metadata dispatch (the latent bug: request-level was being missed) ────────


def _ctx(*, request_meta=None, message_meta=None):
    msg = None
    if message_meta is not None:
        msg = type("Msg", (), {"metadata": message_meta})()
    return type("Ctx", (), {"metadata": request_meta, "message": msg})()


def test_skill_hint_read_from_request_level_metadata():
    # The hub puts skillHint at the request level — message-level only would miss it.
    ctx = _ctx(request_meta={"skillHint": "market_review"}, message_meta=None)
    assert _extract_skill_hint(ctx) == "market_review"


def test_request_level_overrides_message_level():
    ctx = _ctx(request_meta={"skillHint": "req"}, message_meta=None)
    # message-level is a proto Struct in practice; a missing/None one is the
    # common case. Request-level still resolves.
    assert _request_metadata(ctx)["skillHint"] == "req"
    assert _extract_skill_hint(ctx) == "req"


def test_no_skill_hint_returns_empty():
    assert _extract_skill_hint(_ctx(request_meta={}, message_meta=None)) == ""
    assert _extract_skill_hint(_ctx(request_meta=None, message_meta=None)) == ""


def test_caller_trace_reads_request_level():
    ctx = _ctx(request_meta={"a2a.trace": {"traceId": "abc"}}, message_meta=None)
    assert _extract_caller_trace(ctx) == {"traceId": "abc"}
