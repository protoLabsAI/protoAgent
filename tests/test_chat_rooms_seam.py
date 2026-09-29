"""Seam guard for the ``server/chat.py`` @-delegate room extraction (#3838).

The room exchange (``_at_delegate_exchange``) and its parse/note helpers moved to
``server/chat_rooms.py``; ``server.chat`` re-exports every name, so
``server.chat.<name>`` still RESOLVES — but it is a copy of the binding. A monkeypatch
of it there intercepts nothing: ``_pre_turn_dispatch`` calls the exchange through the
``chat_rooms`` module (``_chat_rooms._at_delegate_exchange``) and the moved helpers call
each other by bare name inside ``chat_rooms``. This scans the suite so a stale target
fails loudly: every patch goes to ``server.chat_rooms`` — the names' one home.

The other direction is pinned too: the moved exchange reaches ``server.chat``'s
``_resolve_thread_id`` at CALL time, so a patch there lands.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import runtime.state as rs
import server.chat_rooms as chat_rooms

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

_TESTS = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve()


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def _is_chat_import(node: ast.AST, helpers: set[str]) -> bool:
    """``importlib.import_module("server.chat")`` or a call to a local helper returning it."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name == "import_module":
        return bool(node.args) and isinstance(node.args[0], ast.Constant) and node.args[0].value == "server.chat"
    return name in helpers


def _chat_aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    helpers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(r, ast.Return) and _is_chat_import(r.value, set()) for r in ast.walk(fn))
    }
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases |= {a.asname for a in node.names if a.name == "server.chat" and a.asname}
        elif isinstance(node, ast.Assign) and _is_chat_import(node.value, helpers):
            aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}
    return aliases, helpers


def _is_chat_ref(node: ast.AST, aliases: set[str], helpers: set[str]) -> bool:
    return (isinstance(node, ast.Name) and node.id in aliases) or _is_chat_import(node, helpers)


def test_no_test_patches_moved_room_names_on_server_chat():
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.resolve() == _SELF:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases, helpers = _chat_aliases(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"setattr", "object", "patch", "delattr"}:
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                target = first.value
                if target.startswith("server.chat.") and target.rsplit(".", 1)[1] in _MOVED:
                    stale.append(f"{path.name}:{node.lineno} {target}")
            elif (
                _is_chat_ref(first, aliases, helpers)
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _MOVED
            ):
                stale.append(f"{path.name}:{node.lineno} server.chat.{node.args[1].value}")
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
async def test_exchange_resolves_the_thread_through_server_chat(wired, monkeypatch):
    """A patch of ``server.chat._resolve_thread_id`` must reach the moved
    ``_at_delegate_exchange`` — it resolves it at call time, not at import."""
    import graph.mention_op as mention_op

    chat = _chat()
    seen: dict[str, list] = {"resolve": [], "tid": []}

    def _resolve(md, sid):
        seen["resolve"].append(sid)
        return f"patched:{sid}"

    async def _run_mention(graph, reg, tid, name, rest, **kw):
        seen["tid"].append(tid)
        return {"author": name, "ok": True, "reply": "hi"}

    monkeypatch.setattr(chat, "_resolve_thread_id", _resolve)
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
