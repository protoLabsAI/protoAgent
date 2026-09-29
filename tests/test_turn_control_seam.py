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
reach every ``_CALL_THROUGH`` name (the locks / resolver / HITL hold, and since #3856 the
autonomy classifier, auto-answer cap + sentinel, idle beacon, priority scope and
HITL-resume marker) through ``server.turn_control`` at CALL time — a source scan plus a
break-the-fake test per name — and the HITL hold reaches ``server.chat``'s
``_pending_interrupt_value`` at call time, so a patch on either owner lands.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

import server.turn_control as turn_control
import server.turn_stream as turn_stream
from tests._seam_scan import stale_patches

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
# The names the drivers use — every caller outside turn_control reads them through the
# module (``_turn_control.<name>``), so a patch on the owner is what runs (#3856 added the
# last seven: the drivers used to read those via server.chat's import-time copies).
_CALL_THROUGH = (
    "_thread_lock",
    "_resolve_thread_id",
    "_hold_if_hitl_pending",
    "_is_autonomous",
    "_MAX_AUTONOMOUS_AUTOANSWERS",
    "_AUTONOMOUS_HITL_SENTINEL",
    "_turn_started",
    "_turn_ended",
    "_interactive_turn_priority",
    "_HITL_RESUME",
)

_TESTS = Path(__file__).resolve().parent
_SELF = Path(__file__).resolve()
_REPO = _TESTS.parent


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_or_touches_moved_turn_control_names_on_server_chat():
    stale = stale_patches("server.chat", _MOVED, state=_STATE, exclude=[_SELF])
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
    """No module outside ``turn_control`` reads a ``_CALL_THROUGH`` name by a bare
    (import-time) binding or through ``server.chat`` — only as ``_turn_control.<name>``,
    so a patch on the owner is what runs. Reads, not just calls: the HITL constants are
    compared/passed, never called."""
    offenders: list[str] = []
    for rel in (
        "server/chat.py",
        "server/chat_acp.py",
        "server/chat_session_ops.py",
        "server/chat_rooms.py",
        "server/turn_stream.py",
    ):
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _CALL_THROUGH:
                offenders.append(f"{rel}:{node.lineno} bare {node.id}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in _CALL_THROUGH
                and not (isinstance(node.value, ast.Name) and node.value.id == "_turn_control")
            ):
                offenders.append(f"{rel}:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "reach these through server.turn_control (#3847/#3856): " + ", ".join(offenders)


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


# ── break-the-fake: a patch on turn_control reaches the drivers (#3856) ─────────────────


class _AskThenAnswer:
    """``_run_turn_stream`` stand-in: records each pass's ``resume_value``; the first pass
    parks on a HITL interrupt, later passes answer (or keep asking, ``always_ask``)."""

    def __init__(self, *, always_ask: bool = False):
        self.resume_values: list = []
        self.always_ask = always_ask

    def __call__(self, message, session_id, config, *, resume_value=None, **_kw):
        self.resume_values.append(resume_value)
        ask = self.always_ask or len(self.resume_values) == 1

        async def _gen():
            if ask:
                yield ("input_required", {"question": "which?"})
            else:
                yield ("__raw__", "answered")

        return _gen()


async def _native_frames(chat, request_metadata):
    return [
        f
        async for f in chat._run_native_turn(
            "go", "s-native", {"configurable": {"thread_id": "t-native"}}, request_metadata=request_metadata
        )
    ]


@pytest.mark.asyncio
async def test_native_turn_reads_is_autonomous_from_turn_control(monkeypatch):
    """An operator turn (empty origin) parks — unless turn_control's classifier says
    autonomous, which only reaches the driver if it reads ``_turn_control._is_autonomous``."""
    from runtime.state import STATE

    chat = _chat()
    fake = _AskThenAnswer()
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)
    monkeypatch.setattr(turn_control, "_is_autonomous", lambda md: True)
    frames = await _native_frames(chat, {})
    assert "input_required" not in [k for k, _ in frames]
    assert ("done", "answered") in frames


@pytest.mark.asyncio
async def test_native_turn_reads_the_autoanswer_cap_and_sentinel_from_turn_control(monkeypatch):
    """The auto-answer budget and the no-operator resume value are turn_control's."""
    from runtime.state import STATE

    chat = _chat()
    fake = _AskThenAnswer(always_ask=True)
    sentinel = object()

    async def _clear(config):
        return None

    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(turn_stream, "_run_turn_stream", fake)
    monkeypatch.setattr(chat, "_clear_pending_interrupt", _clear)
    monkeypatch.setattr(turn_control, "_MAX_AUTONOMOUS_AUTOANSWERS", 1)
    monkeypatch.setattr(turn_control, "_AUTONOMOUS_HITL_SENTINEL", sentinel)
    frames = await _native_frames(chat, {"origin": "scheduler"})
    assert [k for k, _ in frames][-1] == "done"
    assert fake.resume_values == [None, sentinel]  # ONE auto-answer, with the patched sentinel


@pytest.mark.asyncio
async def test_streaming_driver_reads_beacon_priority_and_hitl_resume_from_turn_control(monkeypatch):
    """``_chat_langgraph_stream``: the idle beacon, the ADR 0115 priority scope and the
    HITL-resume marker all come from turn_control at call time."""
    import contextlib

    from graph.config import LangGraphConfig
    from runtime.state import STATE

    chat = _chat()
    seen: list = []
    marker = object()

    @contextlib.contextmanager
    def _priority(origin):
        seen.append(("priority", origin))
        yield

    async def _hold(message, session_id, config, *, request_metadata):
        return marker

    async def _native(message, session_id, config, *, request_metadata=None, resume=False, images=None):
        seen.append(("native", resume))
        yield ("done", "ok")

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(chat, "_run_native_turn", _native)
    monkeypatch.setattr(turn_control, "_turn_started", lambda sid: seen.append(("started", sid)))
    monkeypatch.setattr(turn_control, "_turn_ended", lambda sid: seen.append(("ended", sid)))
    monkeypatch.setattr(turn_control, "_interactive_turn_priority", _priority)
    monkeypatch.setattr(turn_control, "_hold_if_hitl_pending", _hold)
    monkeypatch.setattr(turn_control, "_HITL_RESUME", marker)

    frames = [f async for f in chat._chat_langgraph_stream("hello", "s-stream", request_metadata={"origin": "user"})]
    assert frames == [("done", "ok")]
    # The held marker IS turn_control's _HITL_RESUME → the turn resumes instead of re-parking.
    assert seen == [("started", "s-stream"), ("priority", "user"), ("native", True), ("ended", "s-stream")]


@pytest.mark.asyncio
async def test_nonstreaming_driver_reads_beacon_priority_and_hitl_constants_from_turn_control(monkeypatch):
    """``chat()`` → ``_chat_langgraph``: the idle beacon, the priority scope, the
    HITL-resume marker and (goal-driven) the auto-answer cap + sentinel are turn_control's."""
    import contextlib
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.types import Command

    from graph.config import LangGraphConfig
    from runtime.state import STATE

    chat = _chat()
    seen: list = []
    inputs: list = []
    marker, sentinel = object(), object()

    @contextlib.contextmanager
    def _priority(origin):
        seen.append(("priority", origin))
        yield

    async def _hold(message, session_id, config, *, request_metadata):
        return marker

    async def _resume_payload(config, value):
        return f"resume:{value}"

    async def _pending(config):
        return "still asking"

    async def _clear(config):
        seen.append(("cleared",))

    class _Graph:
        async def ainvoke(self, graph_input, config=None):
            inputs.append(graph_input)
            return {"messages": [HumanMessage(content="q"), AIMessage(content="answer")]}

    class _Goals:
        async def parse_control(self, message, session_id, trusted=False):
            return None  # not a /goal command

        def active_goal(self, sid):
            return SimpleNamespace(iteration=1)

        async def evaluate(self, sid, last_text=""):
            return None

    monkeypatch.setattr(STATE, "graph", _Graph(), raising=False)
    monkeypatch.setattr(STATE, "goal_controller", _Goals(), raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(chat, "_resume_payload", _resume_payload)
    monkeypatch.setattr(chat, "_pending_interrupt_value", _pending)
    monkeypatch.setattr(chat, "_clear_pending_interrupt", _clear)
    monkeypatch.setattr(turn_control, "_turn_started", lambda sid: seen.append(("started", sid)))
    monkeypatch.setattr(turn_control, "_turn_ended", lambda sid: seen.append(("ended", sid)))
    monkeypatch.setattr(turn_control, "_interactive_turn_priority", _priority)
    monkeypatch.setattr(turn_control, "_hold_if_hitl_pending", _hold)
    monkeypatch.setattr(turn_control, "_HITL_RESUME", marker)
    monkeypatch.setattr(turn_control, "_MAX_AUTONOMOUS_AUTOANSWERS", 2)
    monkeypatch.setattr(turn_control, "_AUTONOMOUS_HITL_SENTINEL", sentinel)

    out = await chat.chat("hello", "s-sync")
    assert out[0]["content"] == "answer"
    # The held marker IS turn_control's _HITL_RESUME → a real resume, not the "input needed" echo.
    assert isinstance(inputs[0], Command) and inputs[0].resume == "resume:hello"
    # Goal-driven + still parked → exactly the patched cap of auto-answers, each the patched sentinel.
    assert [i.resume for i in inputs[1:]] == [sentinel, sentinel]
    assert seen[0] == ("started", "s-sync") and seen[1][0] == "priority" and seen[-1] == ("ended", "s-sync")
    assert ("cleared",) in seen


@pytest.mark.asyncio
async def test_streaming_overflow_retry_reads_priority_from_turn_control(monkeypatch):
    """The context-overflow retry re-enters the ADR 0115 priority scope — turn_control's."""
    import contextlib

    from graph.config import LangGraphConfig
    from runtime.state import STATE

    chat = _chat()
    origins: list = []
    calls: list = []

    @contextlib.contextmanager
    def _priority(origin):
        origins.append(origin)
        yield

    async def _hold(message, session_id, config, *, request_metadata):
        return None

    async def _native(message, session_id, config, *, request_metadata=None, resume=False, images=None):
        calls.append(message)
        if len(calls) == 1:
            raise RuntimeError("context overflow")
        yield ("done", "recovered")

    async def _compacted(exc, thread_id, session_id):
        return True

    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(), raising=False)
    monkeypatch.setattr(chat, "_run_native_turn", _native)
    monkeypatch.setattr(chat, "_overflow_compacted", _compacted)
    monkeypatch.setattr(turn_control, "_interactive_turn_priority", _priority)
    monkeypatch.setattr(turn_control, "_hold_if_hitl_pending", _hold)

    frames = [f async for f in chat._chat_langgraph_stream("hello", "s-ovf", request_metadata={"origin": "user"})]
    assert frames[-1] == ("done", "recovered")
    assert origins == ["user", "user"]  # the initial turn AND the retry
