"""Conversation origin — who started an A2A thread (docs/design/other-conversations.md §8).

Slice S1 of "Other conversations". Two halves:

  - **Receiver** (``a2a_impl``): the FIRST turn of a context stores the display-only origin
    fields its request metadata carries — ``caller``, ``caller_kind`` (``person``/``agent``),
    ``on_behalf_of``, ``title`` — on the task, read back per context by the (later) list
    endpoint. Later turns never overwrite them; an unrecognised ``caller_kind`` is dropped;
    ``title`` is one clipped line; a private (incognito/hidden) turn stores nothing.
  - **Sender** (``plugins/delegates``): ``delegate_to`` stamps ``caller_kind: "agent"`` on
    its outgoing A2A provenance (always — a delegation is agent-to-agent) and its one-line
    ``summary`` as the thread ``title`` (only when non-empty), at request AND message level.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
import pytest

import plugins.delegates as P
import runtime.state as rs
from a2a_impl import conversation_origin as co
from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine, resolve_task_owner
from plugins.delegates import conversations
from plugins.delegates.a2a import A2aAdapter
from plugins.delegates.registry import DelegateRegistry
from tests._a2a_app import A2A_HEADERS, build_app, drain_handlers, poll_terminal


# ── the normaliser (``conversation_origin.origin_fields``) ──────────────────────


def test_origin_fields_keeps_all_four_normalised():
    md = {"caller": "Claude Code", "caller_kind": "person", "on_behalf_of": "Josh", "title": "Friction triage"}
    assert co.origin_fields(md) == md


def test_origin_fields_drops_unrecognised_caller_kind_keeps_the_rest():
    out = co.origin_fields({"caller": "someone", "caller_kind": "robot", "title": "hi"})
    assert out == {"caller": "someone", "title": "hi"}


def test_origin_fields_accepts_only_person_and_agent():
    assert co.origin_fields({"caller_kind": "agent"}) == {"caller_kind": "agent"}
    assert co.origin_fields({"caller_kind": "person"}) == {"caller_kind": "person"}
    assert co.origin_fields({"caller_kind": "Person"}) == {}  # wrong case — the server never guesses
    assert co.origin_fields({"caller_kind": "  agent "}) == {"caller_kind": "agent"}  # collapsed, then matched


def test_origin_fields_clips_title_to_one_line_of_80():
    out = co.origin_fields({"title": "x" * 200})
    assert len(out["title"]) == 80 and out["title"].endswith("…")


def test_origin_fields_collapses_multiline_values_to_one_line():
    out = co.origin_fields({"caller": "line one\nline two", "title": "a\n\n  b   c "})
    assert out == {"caller": "line one line two", "title": "a b c"}


def test_origin_fields_empty_when_no_fields():
    assert co.origin_fields({}) == co.origin_fields({"unrelated": "x"}) == co.origin_fields(None) == {}


@pytest.mark.parametrize("flag", ["incognito", "hidden"])
def test_origin_fields_empty_for_a_private_turn(flag):
    assert co.origin_fields({"caller": "Claude Code", "caller_kind": "person", "title": "x", flag: True}) == {}


# ── the receiver: persistence + read-back per context (real a2a app) ────────────


async def _done_stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
    yield ("done", "ok")


@contextlib.asynccontextmanager
async def _durable_client(tmp_path):
    """A real a2a app over a SQLite-backed task store; yields ``(client, engine)`` and the
    engine is the one the read-back queries."""
    engine = make_sqlite_engine(str(tmp_path / "a2a-tasks.db"))
    store = ReasoningCoalescingTaskStore(engine, owner_resolver=resolve_task_owner)
    await store.initialize()
    app = build_app(_done_stream, task_store=store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=10) as c:
            yield c, engine
    finally:
        await drain_handlers()
        await engine.dispose()


async def _run_turn(client, *, context_id, metadata=None, message_id, rpc_id):
    """Drive one A2A turn to completion on ``context_id``."""
    message: dict = {"messageId": message_id, "role": "ROLE_USER", "parts": [{"text": "hi"}], "contextId": context_id}
    if metadata is not None:
        message["metadata"] = metadata
    r = await client.post(
        "/a2a",
        headers=A2A_HEADERS,
        json={"jsonrpc": "2.0", "id": rpc_id, "method": "SendMessage", "params": {"message": message}},
    )
    await poll_terminal(client, r.json()["result"]["task"]["id"])


async def test_first_turn_stores_origin_readable_per_context(tmp_path):
    origin = {"caller": "Claude Code", "caller_kind": "person", "on_behalf_of": "Josh", "title": "T" * 150}
    async with _durable_client(tmp_path) as (c, engine):
        await _run_turn(c, context_id="conv-1", metadata=origin, message_id="m1", rpc_id="r1")
        stored = await co.read_origin(engine, "conv-1")
    # Readable per context, with the title clipped to one line of 80.
    assert {k: stored[k] for k in ("caller", "caller_kind", "on_behalf_of")} == {
        "caller": "Claude Code", "caller_kind": "person", "on_behalf_of": "Josh",
    }
    assert len(stored["title"]) == 80 and stored["title"].endswith("…")


async def test_later_turn_does_not_overwrite_the_origin(tmp_path):
    async with _durable_client(tmp_path) as (c, engine):
        await _run_turn(
            c, context_id="conv-2",
            metadata={"caller": "Claude Code", "caller_kind": "person", "title": "first"}, message_id="m1", rpc_id="r1",
        )
        # A second turn on the SAME context with DIFFERENT origin metadata.
        await _run_turn(
            c, context_id="conv-2",
            metadata={"caller": "someone else", "caller_kind": "agent", "title": "second"}, message_id="m2", rpc_id="r2",
        )
        stored = await co.read_origin(engine, "conv-2")
    # The thread's origin is the FIRST turn's — the second never overwrote it.
    assert stored == {"caller": "Claude Code", "caller_kind": "person", "title": "first"}


@pytest.mark.parametrize(
    "metadata",
    [
        None,  # no metadata at all
        {"caller": "Claude Code", "caller_kind": "person", "title": "secret", "incognito": True},
        {"caller": "Claude Code", "caller_kind": "person", "title": "secret", "hidden": True},
    ],
    ids=["no-metadata", "incognito", "hidden"],
)
async def test_turn_stores_no_origin(tmp_path, metadata):
    async with _durable_client(tmp_path) as (c, engine):
        await _run_turn(c, context_id="conv-3", metadata=metadata, message_id="m1", rpc_id="r1")
        assert await co.read_origin(engine, "conv-3") == {}


# ── the sender: delegate_to's outgoing A2A provenance ───────────────────────────


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"jsonrpc": "2.0", "result": {"text": "ok"}}


class _CaptureClient:
    """A fake httpx client that records every posted JSON-RPC body and answers SendMessage
    with an immediate text result (so dispatch returns without the GetTask poll)."""

    bodies: list = []

    def __init__(self, **_kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def post(self, url, json=None, headers=None):
        type(self).bodies.append(json)
        return _Resp()


def _install_capture(monkeypatch):
    class _C(_CaptureClient):
        bodies = []

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _C(**kw))
    return _C.bodies


@pytest.fixture
def _dispatch_env(monkeypatch):
    """Allow the url, no ambient agent name, no real sleeps, immediate text extraction."""
    monkeypatch.setattr("security.policy.check_url", lambda *_a, **_k: None)
    monkeypatch.setattr("plugins.delegates.a2a._own_agent_name", lambda: "")
    monkeypatch.setattr("tools.a2a_parse._extract_text", lambda result, *a, **k: "ok" if result else "")

    async def _noop(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop)
    return monkeypatch


@pytest.mark.parametrize("summary,title", [("Fix the login bug", "Fix the login bug"), ("", None)])
def test_delegate_dispatch_stamps_caller_kind_and_title(_dispatch_env, summary, title):
    bodies = _install_capture(_dispatch_env)
    d = A2aAdapter().parse({"name": "peer", "type": "a2a", "url": "http://127.0.0.1:9/a2a"})
    assert asyncio.run(A2aAdapter().dispatch(d, "do the thing", summary=summary)) == "ok"

    send = next(b for b in bodies if b.get("method") == "SendMessage")
    # Both request level (preferred by the receiver's merge) and message level (fallback).
    for meta in (send["params"]["metadata"], send["params"]["message"]["metadata"]):
        assert meta["caller_kind"] == "agent"  # always — a delegation is agent-to-agent
        if title is None:
            assert "title" not in meta  # an empty summary sends no title
        else:
            assert meta["title"] == title


# ── the sender: delegate_to binds ``summary`` for the dispatch (fore + back) ─────


def _register(delegates, monkeypatch):
    monkeypatch.setattr(P, "_load_delegates_config", lambda: delegates)
    from graph.plugins.host import PluginHost

    class _Reg:
        def __init__(self):
            self.config = {}
            self.tools = []
            self.host = PluginHost()

        def register_tool(self, t):
            self.tools.append(t)

    r = _Reg()
    P.register(r)
    return r


async def _invoke(tool, args):
    result = await tool.ainvoke({"name": "delegate_to", "args": args, "id": "test-call", "type": "tool_call"})
    return getattr(result, "content", result)


@pytest.mark.parametrize("extra", [{}, {"background": True}], ids=["foreground", "background"])
async def test_delegate_to_binds_summary_for_dispatch(monkeypatch, extra):
    """``delegate_to`` binds its ``summary`` on the ``delegate_summary`` ContextVar
    ``registry.dispatch`` reads — the send path that stamps it as the a2a title — for both
    the foreground room path and the background (no-manager inline) path."""
    monkeypatch.setattr(rs.STATE, "background_mgr", None, raising=False)
    seen: dict = {}

    async def _capture(self, name, query, **kwargs):
        seen["summary"] = conversations.current_summary()
        return "ok"

    monkeypatch.setattr(DelegateRegistry, "dispatch", _capture)
    tool = _register([{"name": "peer", "type": "a2a", "url": "http://127.0.0.1:9/a2a"}], monkeypatch).tools[0]

    await _invoke(tool, {"target": "peer", "query": "do it", "summary": "Release prep", **extra})
    assert seen["summary"] == "Release prep"
