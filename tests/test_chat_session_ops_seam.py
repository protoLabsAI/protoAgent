"""Seam guard for the ``server/chat.py`` session-ops + telemetry extraction (#3810).

The session ops moved to ``server/chat_session_ops.py`` and the non-streaming usage
helpers to ``server/turn_telemetry.py``; ``server.chat`` re-exports both, so
``server.chat.<name>`` still RESOLVES — but a monkeypatch on ``server.chat`` no longer
INTERCEPTS a call made from inside the moved code, which looks its names up in its own
module's globals. Such a patch is dead and the test around it can pass silently
against the real function. This scans the suite so a stale target fails loudly.

(A patch on ``operator_api.chat_routes`` — ``cr.compact_session`` etc. — is fine: the
routes call their own module-level binding, and that is what those tests patch.)

The other direction is pinned too: the moved gestures reach ``server.turn_control``'s
``_thread_lock`` / ``_resolve_thread_id`` (their owner since #3847) at CALL time, so a
patch there still lands.
"""

from __future__ import annotations

import asyncio
import importlib

import server.chat_session_ops as session_ops
import server.turn_telemetry as turn_telemetry
from tests._seam_scan import stale_patches

# ``server.chat`` name → the module that now owns the live binding.
_MOVED = {
    **{
        name: session_ops
        for name in (
            "_artifact_resolver",
            "_build_bundle",
            "_compaction_message",
            "_export_message",
            "_fork_message",
            "_publish_message",
            "_publish_preview_message",
            "_rewind_message",
            "aside_session",
            "compact_session",
            "export_session",
            "forget_delegate_conversations",
            "forget_delegate_conversations_for_session",
            "fork_session",
            "publish_preview",
            "publish_session",
            "revoke_published_link",
            "rewind_session",
        )
    },
    "_make_usage_callback": turn_telemetry,
    "_record_local_turn": turn_telemetry,
    "_sum_usage": turn_telemetry,
    "_telemetry_usage": turn_telemetry,
}


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_a_moved_name_on_server_chat():
    stale = stale_patches("server.chat", _MOVED)
    assert not stale, (
        "patch these on server.chat_session_ops / server.turn_telemetry, not server.chat (#3810): "
        + ", ".join(stale)
    )


def test_re_exports_are_the_same_objects():
    chat = _chat()
    for name, owner in _MOVED.items():
        public = name.lstrip("_") if owner is turn_telemetry else name
        assert getattr(chat, name) is getattr(owner, public), name


def test_the_route_module_binds_the_real_session_ops():
    """operator_api.chat_routes imports these from server.chat (the one sanctioned edge);
    what it binds must be the live function, not a stale copy."""
    import operator_api.chat_routes as cr

    for name in ("compact_session", "export_session", "publish_session", "rewind_session", "fork_session"):
        assert getattr(cr, name) is getattr(session_ops, name), name


def test_moved_gestures_call_the_thread_collaborators_through_turn_control(monkeypatch):
    """A patch of ``server.turn_control._resolve_thread_id`` / ``_thread_lock`` (their owner,
    #3847) must reach the moved code — it resolves them at call time rather than binding
    them at import."""
    import runtime.state as rs

    turn_control = importlib.import_module("server.turn_control")
    seen: dict[str, list] = {"resolve": [], "lock": []}
    real_lock = turn_control._thread_lock

    def _resolve(md, sid):
        seen["resolve"].append(sid)
        return f"patched:{sid}"

    def _lock(tid):
        seen["lock"].append(tid)
        return real_lock(tid)

    async def _fake_compact(graph, cp, ks, cfg, tid, sid, **_kw):
        return {"reason": "too_short", "kept": 1, "removed": 0, "refused": False, "thread": tid}

    import graph.compaction_op as compaction_op

    monkeypatch.setattr(turn_control, "_resolve_thread_id", _resolve)
    monkeypatch.setattr(turn_control, "_thread_lock", _lock)
    monkeypatch.setattr(compaction_op, "compact_thread", _fake_compact)
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    out = asyncio.run(session_ops.compact_session("s1"))
    assert out["thread"] == "patched:s1"
    assert seen == {"resolve": ["s1"], "lock": ["patched:s1"]}
