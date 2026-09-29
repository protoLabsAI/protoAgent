"""Seam guard for the ``server/chat.py`` session-ops + telemetry extraction (#3810).

The session ops moved to ``server/chat_session_ops.py`` and the non-streaming usage
helpers to ``server/turn_telemetry.py``; ``server.chat`` re-exports both, so
``server.chat.<name>`` still RESOLVES — but a monkeypatch on ``server.chat`` no longer
INTERCEPTS a call made from inside the moved code, which looks its names up in its own
module's globals. Such a patch is dead and the test around it can pass silently
against the real function. This scans the suite so a stale target fails loudly.

(A patch on ``operator_api.chat_routes`` — ``cr.compact_session`` etc. — is fine: the
routes call their own module-level binding, and that is what those tests patch.)

The other direction is pinned too: the moved gestures reach ``server.chat``'s
``_thread_lock`` / ``_resolve_thread_id`` at CALL time, so a patch there still lands.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
from pathlib import Path

import server.chat_session_ops as session_ops
import server.turn_telemetry as turn_telemetry

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

_TESTS = Path(__file__).resolve().parent


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


def test_no_test_patches_a_moved_name_on_server_chat():
    stale: list[str] = []
    for path in sorted(_TESTS.rglob("test_*.py")):
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
                ((isinstance(first, ast.Name) and first.id in aliases) or _is_chat_import(first, helpers))
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _MOVED
            ):
                stale.append(f"{path.name}:{node.lineno} server.chat.{node.args[1].value}")
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


def test_moved_gestures_call_the_thread_collaborators_through_server_chat(monkeypatch):
    """A patch of ``server.chat._resolve_thread_id`` / ``_thread_lock`` must reach the moved
    code — it resolves them at call time rather than binding them at import."""
    import runtime.state as rs

    chat = _chat()
    seen: dict[str, list] = {"resolve": [], "lock": []}
    real_lock = chat._thread_lock

    def _resolve(md, sid):
        seen["resolve"].append(sid)
        return f"patched:{sid}"

    def _lock(tid):
        seen["lock"].append(tid)
        return real_lock(tid)

    async def _fake_compact(graph, cp, ks, cfg, tid, sid, **_kw):
        return {"reason": "too_short", "kept": 1, "removed": 0, "refused": False, "thread": tid}

    import graph.compaction_op as compaction_op

    monkeypatch.setattr(chat, "_resolve_thread_id", _resolve)
    monkeypatch.setattr(chat, "_thread_lock", _lock)
    monkeypatch.setattr(compaction_op, "compact_thread", _fake_compact)
    monkeypatch.setattr(rs.STATE, "graph", object(), raising=False)

    out = asyncio.run(session_ops.compact_session("s1"))
    assert out["thread"] == "patched:s1"
    assert seen == {"resolve": ["s1"], "lock": ["patched:s1"]}
