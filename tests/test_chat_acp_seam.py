"""Seam guard for the ``server/chat.py`` ACP-runtime extraction (#3828).

The ACP runtime registry (``_ACP_RUNTIMES`` / ``_ACP_RUNTIME_ACCESS`` / ``_ACP_BUSY`` /
``_ACP_LOCK`` + the TTL/cap knobs) and the turn driving moved to
``server/chat_acp.py``; ``server.chat`` re-exports every name, so
``server.chat.<name>`` still RESOLVES — but it is a copy of the binding. A monkeypatch
of it there intercepts nothing (the moved code reads its own module's globals), and a
registry dict swapped in there is a SECOND registry the live code never sees. This
scans the suite so a stale target fails loudly: every patch, and every touch of the
mutable registry state, goes to ``server.chat_acp`` — its one home.

The other direction is pinned too: the moved non-streaming turn reaches
``server.turn_control``'s ``_thread_lock`` / ``_resolve_thread_id`` (their owner since
#3847) at CALL time, and both
drivers (streaming + non-streaming) call the ACP helpers through the ``chat_acp``
module, so a patch there lands.
"""

from __future__ import annotations

import importlib
import types
from pathlib import Path

import server.chat_acp as chat_acp
from tests._seam_scan import stale_patches

# Every name that moved — ``server.chat`` re-exports each as the same object.
_MOVED = (
    "_ACP_BUSY",
    "_ACP_IDLE_TTL_S",
    "_ACP_LOCK",
    "_ACP_MAX_RUNTIMES",
    "_ACP_RUNTIME_ACCESS",
    "_ACP_RUNTIMES",
    "_acp_acquire",
    "_acp_drive_turn",
    "_acp_release",
    "_acp_turn_collected",
    "_evict_acp_runtimes",
    "_get_acp_runtime",
    "_get_acp_runtime_locked",
    "acp_sessions_snapshot",
)
# Module-level mutable state: even READING/mutating it through ``server.chat`` is a trap
# (it silently diverges the moment a test swaps the dict in on ``server.chat_acp``).
_STATE = tuple(n for n in _MOVED if n.startswith("_ACP_"))

_SELF = Path(__file__).resolve()


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_or_touches_moved_acp_names_on_server_chat():
    stale = stale_patches("server.chat", _MOVED, state=_STATE, exclude=[_SELF])
    assert not stale, "patch/touch these on server.chat_acp, not server.chat (#3828): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    chat = _chat()
    for name in _MOVED:
        assert getattr(chat, name) is getattr(chat_acp, name), name


def test_server_package_binds_the_real_snapshot():
    """``server/__init__`` wires ``GET /api/acp/sessions`` to ``acp_sessions_snapshot``."""
    import server

    assert server.acp_sessions_snapshot is chat_acp.acp_sessions_snapshot


def test_collected_turn_calls_the_thread_collaborators_through_turn_control(monkeypatch):
    """A patch of ``server.turn_control._resolve_thread_id`` / ``_thread_lock`` (their owner,
    #3847) must reach the moved ``_acp_turn_collected`` — it resolves them at call time."""
    import asyncio

    turn_control = importlib.import_module("server.turn_control")
    seen: dict[str, list] = {"resolve": [], "lock": [], "acquire": []}
    real_lock = turn_control._thread_lock

    def _resolve(md, sid):
        seen["resolve"].append(sid)
        return f"patched:{sid}"

    def _lock(tid):
        seen["lock"].append(tid)
        return real_lock(tid)

    async def _acquire(tid):
        seen["acquire"].append(tid)
        return types.SimpleNamespace(agent="mock")

    async def _release(tid):
        return None

    async def _drive(rt, message):
        yield ("done", "ok")

    monkeypatch.setattr(turn_control, "_resolve_thread_id", _resolve)
    monkeypatch.setattr(turn_control, "_thread_lock", _lock)
    monkeypatch.setattr(chat_acp, "_acp_acquire", _acquire)
    monkeypatch.setattr(chat_acp, "_acp_release", _release)
    monkeypatch.setattr(chat_acp, "_acp_drive_turn", _drive)

    out = asyncio.run(chat_acp._acp_turn_collected("s1", "hi"))
    assert out == [{"role": "assistant", "content": "ok"}]
    assert seen == {"resolve": ["s1"], "lock": ["patched:s1"], "acquire": ["patched:s1"]}


async def test_streaming_driver_calls_the_acp_helpers_through_chat_acp(monkeypatch):
    """The streaming driver's ACP branch reaches ``_acp_acquire`` / ``_acp_drive_turn`` /
    ``_acp_release`` through the ``chat_acp`` module at call time, so a patch there lands."""
    from runtime.state import STATE

    chat = _chat()
    calls: list[tuple[str, str]] = []

    async def _acquire(tid):
        calls.append(("acquire", tid))
        return types.SimpleNamespace(agent="mock")

    async def _release(tid):
        calls.append(("release", tid))

    async def _drive(rt, message):
        calls.append(("drive", message))
        yield ("done", "via-chat-acp")

    monkeypatch.setattr(
        STATE,
        "graph_config",
        types.SimpleNamespace(agent_runtime="acp:codex", operator_mcp_tools=[], acp_agents={}),
        raising=False,
    )
    monkeypatch.setattr(STATE, "graph", object(), raising=False)
    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(chat_acp, "_acp_acquire", _acquire)
    monkeypatch.setattr(chat_acp, "_acp_release", _release)
    monkeypatch.setattr(chat_acp, "_acp_drive_turn", _drive)

    frames = [f async for f in chat._chat_langgraph_stream("plain message", "sess-seam")]
    assert ("done", "via-chat-acp") in frames
    assert [c[0] for c in calls] == ["acquire", "drive", "release"]
