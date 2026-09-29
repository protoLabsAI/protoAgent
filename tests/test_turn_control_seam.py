"""Seam guard for the ``server/chat.py`` turn-control extraction (#3847).

Thread locks, the thread-id resolver, origin classification, session attendance, the
server-turn control plane, the HITL hold and the idle beacon moved to
``server/turn_control.py``; ``server.chat`` re-exports the names, so
``server.chat.<name>`` still RESOLVES — but it is a copy of the binding. A monkeypatch
of it there intercepts nothing (the drivers call the patched helpers through
``_turn_control.<name>``, and the moved code reads its own module's globals), and
module-level state swapped in there is a SECOND registry the live code never sees. This
scans the suite so a stale target fails loudly: every patch, and every touch of the
mutable state, goes to ``server.turn_control`` — its one home.

The rebound idle-beacon ints (``_ACTIVE_TURNS`` / ``_LAST_TURN_MONOTONIC``) are not
re-exported at all: ``global`` rebinding in ``_turn_started`` would leave any copy stale.

The other direction is pinned too: both turn drivers and the sibling ``chat_*`` modules
reach ``_thread_lock`` / ``_resolve_thread_id`` / ``_hold_if_hitl_pending`` through
``server.turn_control`` at CALL time, and the HITL hold reaches ``server.chat``'s
``_pending_interrupt_value`` at call time, so a patch on either owner lands.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import server.turn_control as turn_control

# Every name ``server.chat`` re-exports from ``server.turn_control`` — the same object.
_REEXPORTED = (
    "_ATTENDANCE_CONDITIONAL_ORIGINS",
    "_ATTENDED_SESSIONS",
    "_ATTENDED_SESSIONS_MAX",
    "_AUTONOMOUS_HITL_SENTINEL",
    "_AUTONOMOUS_ORIGINS",
    "_CONTROL_ORIGINS",
    "_HITL_RESUME",
    "_INTERACTIVE_ORIGINS",
    "_LIVE_SERVER_TURNS",
    "_LiveServerTurn",
    "_MAX_AUTONOMOUS_AUTOANSWERS",
    "_THREAD_LOCKS",
    "_background_resume_attended",
    "_control_origin",
    "_hold_if_hitl_pending",
    "_interactive_turn_priority",
    "_is_autonomous",
    "_is_hitl_resume",
    "_resolve_thread_id",
    "_server_turn_key",
    "_thread_lock",
    "_truthy",
    "_turn_ended",
    "_turn_started",
    "active_turns",
    "attendance_stream",
    "finish_live_server_turn",
    "is_autonomous_origin",
    "is_interactive_origin",
    "is_session_attended",
    "live_server_turn_control",
    "mark_session_attended",
    "register_live_server_turn",
    "release_session_attended",
    "seconds_since_last_turn",
    "server_turn_control_payload",
    "submit_server_turn_interjection",
)
# Owned by turn_control and deliberately NOT re-exported (a copy of a rebound int is stale).
_NOT_REEXPORTED = ("_ACTIVE_TURNS", "_LAST_TURN_MONOTONIC")
_MOVED = _REEXPORTED + _NOT_REEXPORTED
# Module-level mutable state: even READING/mutating it through ``server.chat`` is a trap.
_STATE = (
    "_ACTIVE_TURNS",
    "_ATTENDED_SESSIONS",
    "_ATTENDED_SESSIONS_MAX",
    "_LAST_TURN_MONOTONIC",
    "_LIVE_SERVER_TURNS",
    "_THREAD_LOCKS",
)
# The helpers tests patch — every caller outside turn_control goes through the module.
_CALL_THROUGH = ("_thread_lock", "_resolve_thread_id", "_hold_if_hitl_pending")

_TESTS = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve()
_REPO = _TESTS.parent


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


def test_no_test_patches_or_touches_moved_turn_control_names_on_server_chat():
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
        if path.resolve() == _SELF:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases, helpers = _chat_aliases(tree)
        for node in ast.walk(tree):
            # ``chat._ATTENDED_SESSIONS.clear()`` … — state read/mutated through the copy.
            if isinstance(node, ast.Attribute) and node.attr in _STATE and _is_chat_ref(node.value, aliases, helpers):
                stale.append(f"{path.name}:{node.lineno} server.chat.{node.attr}")
                continue
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
    assert not stale, "patch/touch these on server.turn_control, not server.chat (#3847): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    chat = _chat()
    for name in _REEXPORTED:
        assert getattr(chat, name) is getattr(turn_control, name), name


def test_rebound_beacon_ints_are_not_re_exported():
    """A re-export of a ``global``-rebound int is a stale snapshot — it must not exist."""
    chat = _chat()
    for name in _NOT_REEXPORTED:
        assert not hasattr(chat, name), name
        assert hasattr(turn_control, name), name


def test_server_package_and_route_module_bind_the_real_objects():
    """``server/__init__`` re-exports the public names via ``server.chat``, and
    ``operator_api.chat_routes`` imports ``_resolve_thread_id`` from ``server.chat`` (the
    one sanctioned edge): what each binds must be the live object, not a copy."""
    import operator_api.chat_routes as cr
    import server

    for name in (
        "attendance_stream",
        "finish_live_server_turn",
        "is_autonomous_origin",
        "is_session_attended",
        "live_server_turn_control",
        "mark_session_attended",
        "register_live_server_turn",
        "release_session_attended",
        "submit_server_turn_interjection",
    ):
        assert getattr(server, name) is getattr(turn_control, name), name
    assert cr._resolve_thread_id is turn_control._resolve_thread_id


def test_callers_reach_the_patched_helpers_through_turn_control():
    """No module outside ``turn_control`` calls ``_thread_lock`` / ``_resolve_thread_id`` /
    ``_hold_if_hitl_pending`` by a bare (import-time) binding or through ``server.chat`` —
    only as ``_turn_control.<name>(…)``, so a patch on the owner is what runs."""
    offenders: list[str] = []
    for rel in ("server/chat.py", "server/chat_acp.py", "server/chat_session_ops.py", "server/chat_rooms.py"):
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id in _CALL_THROUGH:
                offenders.append(f"{rel}:{node.lineno} bare {func.id}()")
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in _CALL_THROUGH
                and not (isinstance(func.value, ast.Name) and func.value.id == "_turn_control")
            ):
                offenders.append(f"{rel}:{node.lineno} {ast.unparse(func)}()")
    assert not offenders, "call these through server.turn_control (#3847): " + ", ".join(offenders)


@pytest.mark.asyncio
async def test_nonstreaming_driver_calls_turn_control_at_call_time(monkeypatch):
    """A patch of ``server.turn_control._resolve_thread_id`` / ``_thread_lock`` /
    ``_hold_if_hitl_pending`` reaches the non-streaming driver behind ``chat()``."""
    from langchain_core.messages import AIMessage, HumanMessage

    from graph.config import LangGraphConfig
    from runtime.state import STATE

    chat = _chat()
    seen: dict[str, list] = {"resolve": [], "lock": [], "hold": []}
    real_lock = turn_control._thread_lock

    def _resolve(md, sid):
        seen["resolve"].append(sid)
        return f"patched:{sid}"

    def _lock(tid):
        seen["lock"].append(tid)
        return real_lock(tid)

    async def _hold(message, session_id, config, *, request_metadata):
        seen["hold"].append(config["configurable"]["thread_id"])
        return None

    class _Graph:
        async def ainvoke(self, graph_input, config=None):
            return {"messages": [HumanMessage(content="q"), AIMessage(content="answer")]}

    monkeypatch.setattr(STATE, "graph", _Graph(), raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(turn_control, "_resolve_thread_id", _resolve)
    monkeypatch.setattr(turn_control, "_thread_lock", _lock)
    monkeypatch.setattr(turn_control, "_hold_if_hitl_pending", _hold)

    out = await chat.chat("hello", "s-seam")
    assert out[0]["content"] == "answer"
    assert seen["resolve"] and set(seen["resolve"]) == {"s-seam"}
    assert "patched:s-seam" in seen["lock"]
    assert seen["hold"] == ["patched:s-seam"]


@pytest.mark.asyncio
async def test_hitl_hold_reads_the_pending_interrupt_through_server_chat(monkeypatch):
    """The moved hold reaches ``server.chat._pending_interrupt_value`` at call time."""
    from graph import steering

    chat = _chat()

    async def _pending(config):
        return "form?"

    monkeypatch.setattr(chat, "_pending_interrupt_value", _pending)
    try:
        held = await turn_control._hold_if_hitl_pending(
            "later", "s-hold", {"configurable": {"thread_id": "t"}}, request_metadata={}
        )
        assert held == "form?"
        assert steering.pending("s-hold") == 1
        resume = await turn_control._hold_if_hitl_pending(
            "answer", "s-hold", {"configurable": {"thread_id": "t"}}, request_metadata={"hitl_resume": True}
        )
        assert resume is turn_control._HITL_RESUME
    finally:
        steering._QUEUES.pop("s-hold", None)


@pytest.mark.asyncio
async def test_idle_beacon_has_one_home(monkeypatch):
    """The drivers bracket turns through the (re-exported) ``_turn_started`` / ``_turn_ended``,
    which rebind turn_control's globals — and the auto-update idle gate reads them there."""
    import server.maintenance_loops as maintenance_loops

    chat = _chat()
    monkeypatch.setattr(turn_control, "_ACTIVE_TURNS", 0)
    seen: list[int] = []

    async def _impl(message, session_id, **kw):
        seen.append(turn_control._ACTIVE_TURNS)
        seen.append(int(maintenance_loops._server_is_idle()))
        yield ("done", "ok")

    monkeypatch.setattr(chat, "_chat_langgraph_stream_impl", _impl)
    assert [ev async for ev in chat._chat_langgraph_stream("hi", "s-beacon")] == [("done", "ok")]
    assert seen == [1, 0]  # in flight → not idle
    assert turn_control._ACTIVE_TURNS == 0
