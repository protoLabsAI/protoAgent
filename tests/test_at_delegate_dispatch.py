"""Server-side @-delegate dispatch (S1).

A chat message that OPENS with ``@<delegate-name>`` is routed straight to that
delegate over ``DelegateRegistry.dispatch()`` — short-circuiting the LLM turn —
BEFORE the goal-control / slash-command cascade. These cover the parser
(``_parse_at_delegate``), the shared dispatch decision (``_at_delegate_reply``),
and an end-to-end run through both the non-streaming (``chat``) and streaming
(``_chat_langgraph_stream_impl``) turn drivers proving the delegate is dispatched
and no model turn is started.
"""

from __future__ import annotations

import runtime.state as rs
from plugins.delegates.registry import DelegateRegistry
from server.chat import (
    _at_delegate_reply,
    _parse_at_delegate,
)


def _install_registry(monkeypatch, names=("proto",)):
    """Publish a live DelegateRegistry (roster = ``names``) on STATE, the way the
    delegates plugin does at register() time."""
    reg = DelegateRegistry(
        [{"name": n, "type": "openai", "url": "https://g/v1", "model": "m"} for n in names]
    )
    monkeypatch.setattr(rs.STATE, "delegate_registry", reg, raising=False)
    return reg


class _NoTurnGraph:
    """Stands in for the compiled graph and fails loudly if a turn is ever driven —
    an @-delegate dispatch must short-circuit before any model call."""

    def __init__(self):
        self.turns = 0

    async def ainvoke(self, *a, **k):
        self.turns += 1
        raise AssertionError("no LLM turn should start for an @-delegate dispatch")

    def astream_events(self, *a, **k):
        self.turns += 1
        raise AssertionError("no LLM turn should start for an @-delegate dispatch")


def _wire_no_turn(monkeypatch):
    """Set up STATE so a turn would reach the graph (which fails), letting the tests
    assert the @-dispatch short-circuited instead."""
    from graph.config import LangGraphConfig

    g = _NoTurnGraph()
    monkeypatch.setattr(rs.STATE, "graph", g, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", None, raising=False)
    return g


# ── parser: _parse_at_delegate ────────────────────────────────────────────────


def test_parse_at_delegate_valid(monkeypatch):
    _install_registry(monkeypatch)
    assert _parse_at_delegate("@proto fix the test") == ("proto", "fix the test")


def test_parse_at_delegate_case_insensitive(monkeypatch):
    # @Proto matches the registered `proto` and resolves to its REGISTERED name
    # (dispatch/get are case-sensitive).
    _install_registry(monkeypatch)
    assert _parse_at_delegate("@Proto build it") == ("proto", "build it")
    assert _parse_at_delegate("@Proto") == ("proto", "")


def test_parse_at_delegate_no_match(monkeypatch):
    _install_registry(monkeypatch)
    assert _parse_at_delegate("@unknown hello") is None


def test_parse_at_delegate_not_leading(monkeypatch):
    # A mid-message mention is not a dispatch in v1 — the @ must be the first
    # non-whitespace character.
    _install_registry(monkeypatch)
    assert _parse_at_delegate("hello @proto") is None


def test_parse_at_delegate_bare(monkeypatch):
    _install_registry(monkeypatch)
    assert _parse_at_delegate("@proto") == ("proto", "")


def test_parse_at_delegate_no_registry(monkeypatch):
    # Delegates plugin not loaded ⇒ no registry ⇒ @ is ordinary text.
    monkeypatch.setattr(rs.STATE, "delegate_registry", None, raising=False)
    assert _parse_at_delegate("@proto do it") is None


# ── dispatch decision: _at_delegate_reply ─────────────────────────────────────


async def test_at_reply_dispatches_known_delegate(monkeypatch):
    _install_registry(monkeypatch)
    seen = {}

    async def _dispatch(self, name, query, **kwargs):
        seen["call"] = (name, query)
        return "PROTO SAYS HI"

    monkeypatch.setattr(DelegateRegistry, "dispatch", _dispatch)

    assert await _at_delegate_reply("@proto hello") == "PROTO SAYS HI"
    assert seen["call"] == ("proto", "hello")


async def test_at_reply_unknown_names_the_roster(monkeypatch):
    _install_registry(monkeypatch, names=("proto", "codex"))
    reply = await _at_delegate_reply("@nope hello")
    assert reply == "Unknown or unreachable delegate: @nope. Available: proto, codex."


async def test_at_reply_bare_returns_usage(monkeypatch):
    _install_registry(monkeypatch)
    reply = await _at_delegate_reply("@proto")
    assert "Usage:" in reply and "proto" in reply


async def test_at_reply_mid_message_falls_through(monkeypatch):
    _install_registry(monkeypatch)
    assert await _at_delegate_reply("hello @proto how are you") is None


async def test_at_reply_no_registry_falls_through(monkeypatch):
    monkeypatch.setattr(rs.STATE, "delegate_registry", None, raising=False)
    assert await _at_delegate_reply("@proto hello") is None


async def test_at_reply_dispatch_failure_is_an_answer(monkeypatch):
    _install_registry(monkeypatch)

    async def _boom(self, name, query, **kwargs):
        raise RuntimeError("delegate offline")

    monkeypatch.setattr(DelegateRegistry, "dispatch", _boom)
    reply = await _at_delegate_reply("@proto hello")
    assert reply == "Delegate @proto failed: delegate offline"


# ── end-to-end: non-streaming (chat) + streaming drivers ──────────────────────


async def test_at_dispatch_integration_nonstreaming(monkeypatch):
    from server.chat import chat

    _install_registry(monkeypatch)
    g = _wire_no_turn(monkeypatch)
    seen = {}

    async def _dispatch(self, name, query, **kwargs):
        seen["call"] = (name, query)
        return "PROTO SAYS HI"

    monkeypatch.setattr(DelegateRegistry, "dispatch", _dispatch)

    out = await chat("@proto hello", "sessAt")

    # The delegate reply is the assistant message …
    assert out[0]["content"] == "PROTO SAYS HI"
    # … dispatch got the stripped query, keyed by the registered delegate name …
    assert seen["call"] == ("proto", "hello")
    # … and no LLM turn was started.
    assert g.turns == 0


async def test_at_dispatch_integration_streaming(monkeypatch):
    from server.chat import _chat_langgraph_stream_impl

    _install_registry(monkeypatch)
    g = _wire_no_turn(monkeypatch)
    seen = {}

    async def _dispatch(self, name, query, **kwargs):
        seen["call"] = (name, query)
        return "PROTO SAYS HI"

    monkeypatch.setattr(DelegateRegistry, "dispatch", _dispatch)

    frames = [f async for f in _chat_langgraph_stream_impl("@proto hello", "sessAtStream")]

    assert ("done", "PROTO SAYS HI") in frames
    assert seen["call"] == ("proto", "hello")
    assert g.turns == 0


async def test_at_dispatch_unknown_short_circuits_without_a_turn(monkeypatch):
    from server.chat import chat

    _install_registry(monkeypatch)
    g = _wire_no_turn(monkeypatch)

    out = await chat("@unknown hello", "sessAtUnknown")

    assert out[0]["content"] == "Unknown or unreachable delegate: @unknown. Available: proto."
    assert g.turns == 0  # not an LLM turn either
