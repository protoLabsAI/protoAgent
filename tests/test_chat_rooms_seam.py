"""Seam guard for the ``server/chat.py`` @-delegate room extraction (#3838).

The room exchange (``_at_delegate_exchange``) and its parse/note helpers moved to
``server/chat_rooms.py``; ``server.chat`` re-exports every name, so
``server.chat.<name>`` still RESOLVES — but it is a copy of the binding. A monkeypatch
of it there intercepts nothing: ``_pre_turn_dispatch`` calls the exchange through the
``chat_rooms`` module (``_chat_rooms._at_delegate_exchange``) and the moved helpers call
each other by bare name inside ``chat_rooms``. This scans the suite so a stale target
fails loudly: every patch goes to ``server.chat_rooms`` — the names' one home.

The other direction is pinned too: the moved exchange reaches ``server.turn_control``'s
``_resolve_thread_id`` (its owner since #3847) at CALL time, so a patch there lands.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

import runtime.state as rs
import server.chat_rooms as chat_rooms
from tests._seam_scan import stale_patches

# Every name that moved — ``server.chat`` re-exports each as the same object.
_MOVED = (
    "_all_mentions_are_startable_unreachable",
    "_at_delegate_exchange",
    "_at_delegate_reply",
    "_covered_by_a_bubble",
    "_delegate_unavailable_msg",
    "_parse_at_delegate",
    "_parse_at_delegates",
    "_room_note",
    "_with_room_notes",
)

_SELF = Path(__file__).resolve()


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_moved_room_names_on_server_chat():
    stale = stale_patches("server.chat", _MOVED, exclude=[_SELF])
    assert not stale, "patch these on server.chat_rooms, not server.chat (#3838): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    chat = _chat()
    for name in _MOVED:
        assert getattr(chat, name) is getattr(chat_rooms, name), name


class _Delegate:
    type = "acp"
    url = ""


class _Reg:
    def names(self):
        return ["proto"]

    def roster(self):
        return [{"name": "proto", "type": "acp", "description": "", "url": ""}]

    def get(self, name):
        return _Delegate() if name == "proto" else None

    async def dispatch(self, name, query, *, conversation_key=None, permissions=None):
        return "proto says hi"


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(rs.STATE, "delegate_registry", _Reg(), raising=False)
    monkeypatch.setattr(rs.STATE, "graph", None, raising=False)
    monkeypatch.setattr(rs.STATE, "thread_id_resolver", None, raising=False)


@pytest.mark.asyncio
async def test_exchange_resolves_the_thread_through_turn_control(wired, monkeypatch):
    """A patch of ``server.turn_control._resolve_thread_id`` (its owner, #3847) must reach
    the moved ``_at_delegate_exchange`` — it resolves it at call time, not at import."""
    import graph.mention_op as mention_op

    turn_control = importlib.import_module("server.turn_control")
    seen: dict[str, list] = {"resolve": [], "tid": []}

    def _resolve(md, sid):
        seen["resolve"].append(sid)
        return f"patched:{sid}"

    async def _run_mention(graph, reg, tid, name, rest, **kw):
        seen["tid"].append(tid)
        return {"author": name, "ok": True, "reply": "hi"}

    monkeypatch.setattr(turn_control, "_resolve_thread_id", _resolve)
    monkeypatch.setattr(mention_op, "run_mention", _run_mention)

    reply, _outcomes = await chat_rooms._at_delegate_exchange("@proto hello", "s-seam")
    assert reply == "hi"
    assert seen == {"resolve": ["s-seam"], "tid": ["patched:s-seam"]}


@pytest.mark.asyncio
async def test_pre_turn_dispatch_calls_the_exchange_through_chat_rooms(wired, monkeypatch):
    """``_pre_turn_dispatch`` reaches ``_at_delegate_exchange`` through the ``chat_rooms``
    module at call time, so a patch there lands."""
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)
    calls: list[str] = []

    async def _fake(message, session_id="", request_metadata=None):
        calls.append(message)
        return "via-chat-rooms", [{"author": "proto", "ok": True, "reply": "via-chat-rooms"}]

    monkeypatch.setattr(chat_rooms, "_at_delegate_exchange", _fake)

    frames = [f async for f in _chat()._chat_langgraph_stream_impl("@proto hello", "s-seam")]
    assert calls == ["@proto hello"]
    assert frames[-1] == ("done", "via-chat-rooms")
