"""Seam guard for the ``server/chat.py`` streaming event-loop extraction (#3874).

``_run_turn_stream`` and the helpers only it uses (the background-delegation receipt
parsing, the lead-speaker filter, ``_paragraph_break``) moved to
``server/turn_stream.py``; ``server.chat`` re-exports every name, so
``server.chat.<name>`` still RESOLVES — but it is a copy of the binding. A monkeypatch of
it there intercepts nothing (``_run_native_turn`` calls ``_turn_stream._run_turn_stream``
and the moved loop reads its own module's globals). This scans the suite so a stale
target fails loudly: patch these names on ``server.turn_stream``.

The other direction is pinned too: the moved loop reaches every collaborator that STAYED
in ``server.chat`` (the vision message, the background drain, the HITL resume/interrupt
readers, the tool payload shaping) through ``server.chat`` at CALL time — a source scan
plus a break-the-fake test — so a patch on ``server.chat`` still lands; and
``_run_native_turn`` reaches the loop through ``server.turn_stream`` at call time.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import server.turn_stream as turn_stream
from tests._seam_scan import stale_patches

# Every name that moved — ``server.chat`` re-exports each as the same object.
_MOVED = (
    "_BG_DISPATCH_REFUSED",
    "_BG_JOB_ID",
    "_TOOL_NODE",
    "_delegation_summary",
    "_lc_internal_call_marker",
    "_paragraph_break",
    "_run_turn_stream",
    "_speaks_for_the_lead",
)
# Collaborators that stayed in ``server.chat``: the moved loop reads each as
# ``_chat().<name>`` at call time, never by a bare (import-time) binding.
_CALL_THROUGH = (
    "_TOOL_PREVIEW_CHARS",
    "_coerce_room_text",
    "_coerce_tool_output",
    "_coerce_tool_value",
    "_drain_background",
    "_interrupt_payload",
    "_pending_interrupt_value",
    "_resume_payload",
    "_tool_output_chars",
    "_vision_human_message",
)

_SELF = Path(__file__).resolve()
_REPO = _SELF.parent.parent


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_moved_turn_stream_names_on_server_chat():
    stale = stale_patches("server.chat", _MOVED, exclude=[_SELF])
    assert not stale, "patch these on server.turn_stream, not server.chat (#3874): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    import server

    chat = _chat()
    for name in _MOVED:
        assert getattr(chat, name) is getattr(turn_stream, name), name
    assert server._run_turn_stream is turn_stream._run_turn_stream


def test_the_turn_stream_module_imports_without_server_chat():
    """No import-time edge back into ``server.chat`` (it is reached at call time only)."""
    subprocess.run([sys.executable, "-c", "import server.turn_stream"], check=True, cwd=str(_REPO))
    tree = ast.parse(Path(turn_stream.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "server.chat" for n in top)
    assert not any(isinstance(n, ast.Import) and any(a.name == "server.chat" for a in n.names) for n in top)
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module == "server" and any(a.name == "chat" for a in n.names) for n in top
    )


def _is_chat_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_chat"


def test_moved_loop_reads_chat_collaborators_at_call_time():
    """In ``server/turn_stream.py`` every ``_CALL_THROUGH`` name is read only as
    ``_chat().<name>`` — never a bare global (an import-time copy a patch misses)."""
    offenders: list[str] = []
    tree = ast.parse(Path(turn_stream.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _CALL_THROUGH:
            offenders.append(f"turn_stream.py:{node.lineno} bare {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _CALL_THROUGH and not _is_chat_call(node.value):
            offenders.append(f"turn_stream.py:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "reach these through server.chat at call time (#3874): " + ", ".join(offenders)


def test_callers_reach_run_turn_stream_through_turn_stream():
    """No caller in ``server/`` reads ``_run_turn_stream`` by a bare binding — only as
    ``_turn_stream._run_turn_stream``, so a patch on the owner is what runs."""
    offenders: list[str] = []
    for path in sorted((_REPO / "server").rglob("*.py")):
        if path.name == "turn_stream.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "_run_turn_stream":
                offenders.append(f"{path.name}:{node.lineno} bare _run_turn_stream")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "_run_turn_stream"
                and not (isinstance(node.value, ast.Name) and node.value.id == "_turn_stream")
            ):
                offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "call it as _turn_stream._run_turn_stream (#3874): " + ", ".join(offenders)


# ── break-the-fake: patches land at call time ──────────────────────────────────


@pytest.mark.asyncio
async def test_native_turn_calls_the_patched_turn_stream(monkeypatch):
    """A patch of ``server.turn_stream._run_turn_stream`` is what ``_run_native_turn`` runs."""
    from runtime.state import STATE

    chat = _chat()
    calls: list = []

    def _fake(message, session_id, config, **_kw):
        calls.append(message)

        async def _gen():
            yield ("__raw__", "from the patched loop")

        return _gen()

    monkeypatch.setattr(STATE, "goal_controller", None, raising=False)
    monkeypatch.setattr(turn_stream, "_run_turn_stream", _fake)
    frames = [
        f
        async for f in chat._run_native_turn("go", "s-ts", {"configurable": {"thread_id": "t-ts"}}, request_metadata={})
    ]
    assert calls == ["go"]
    assert ("done", "from the patched loop") in frames


@pytest.mark.asyncio
async def test_moved_loop_uses_patched_chat_collaborators(monkeypatch):
    """Every ``_CALL_THROUGH`` collaborator patched on ``server.chat`` is what the moved
    loop runs — and the loop's latency clock is ``server.turn_stream.time``."""
    from langchain_core.messages import HumanMessage

    from runtime.state import STATE
    from tests._turn_driver_fakes import (
        Clock,
        ScriptedGraph,
        model_end,
        set_interrupt,
        tool_end,
        tool_msg,
        tool_start,
    )

    chat = _chat()
    seen: list[str] = []
    clock = Clock()

    def _rec(tag, ret):
        def _f(*a, **k):
            seen.append(tag)
            return ret

        return _f

    async def _resume(config, value):
        seen.append("resume")
        return {"patched": value}

    async def _pending(config):
        seen.append("pending")
        return "patched?"

    graph = ScriptedGraph(
        streams=[
            [
                model_end("m1", tool_calls=[("tc1", "echo", {"a": 1})], output=True),
                tool_start("e1", "echo", {"a": 1}),
                clock.tick(0.25),
                tool_end("e1", "echo", tool_msg("real output", "tc1")),
                tool_start("d1", "delegate_to", {"target": "proto", "query": "look"}),
                tool_end("d1", "delegate_to", tool_msg("real reply", "dx")),
                set_interrupt("real question"),
            ]
        ]
    )
    monkeypatch.setattr(STATE, "graph", graph, raising=False)
    monkeypatch.setattr(turn_stream, "time", clock)
    monkeypatch.setattr(chat, "_vision_human_message", _rec("vision", HumanMessage(content="V")))
    monkeypatch.setattr(chat, "_drain_background", _rec("drain", ([], [])))
    monkeypatch.setattr(chat, "_resume_payload", _resume)
    monkeypatch.setattr(chat, "_coerce_tool_value", _rec("value", "PATCHED-IN"))
    monkeypatch.setattr(chat, "_coerce_tool_output", _rec("output", "PATCHED-OUT"))
    monkeypatch.setattr(chat, "_tool_output_chars", _rec("chars", 4242))
    monkeypatch.setattr(chat, "_coerce_room_text", _rec("room", "PATCHED-ROOM"))
    monkeypatch.setattr(chat, "_pending_interrupt_value", _pending)
    monkeypatch.setattr(chat, "_interrupt_payload", _rec("payload", {"question": "PATCHED-Q"}))
    monkeypatch.setattr(chat, "_TOOL_PREVIEW_CHARS", 3)

    frames = [f async for f in turn_stream._run_turn_stream("hi", "s-cc", {"configurable": {"thread_id": "t"}})]
    assert graph.stream_calls[0][0]["messages"][-1].content == "V"
    assert ("tool_start", {"id": "tc1", "name": "echo", "input": "PATCHED-IN"}) in frames
    end = next(p for k, p in frames if k == "tool_end")
    assert (end["output"], end["output_chars"], end["duration_ms"]) == ("PATCHED-OUT", 4242, 250)
    assert any(k == "room_reply" and p.get("text") == "PATCHED-ROOM" for k, p in frames)
    assert frames[-1] == ("input_required", {"question": "PATCHED-Q"})
    assert {"vision", "drain", "value", "output", "chars", "room", "pending", "payload"} <= set(seen)

    # The resume path reaches the patched ``_resume_payload``; the preview cap is read
    # from server.chat too (a component's card prefix is cut at the patched 3 chars).
    from graph.components import encode_component

    payload = "abcdef " + encode_component("keyvalue", {"items": [{"k": "a", "v": 1}]})
    graph.streams.append(
        [tool_start("c1", "show_component"), tool_end("c1", "show_component", tool_msg(payload, "tc2"))]
    )
    seen.clear()
    monkeypatch.setattr(chat, "_pending_interrupt_value", lambda config: _none())
    frames = [
        f
        async for f in turn_stream._run_turn_stream(
            "x", "s-cc", {"configurable": {"thread_id": "t"}}, resume_value="ans"
        )
    ]
    assert graph.resumes[-1] == {"patched": "ans"} and "resume" in seen
    assert next(p for k, p in frames if k == "tool_end")["output"] == "abc"


async def _none():
    return None
