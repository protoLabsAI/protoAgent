"""Seam guard for the ``server/chat.py`` non-streaming driver extraction (#3917).

``_chat_langgraph_impl`` (with its former closures ``_native_turn`` / ``_last_ai``, now
module-level) and ``_trace_reply_output`` moved to ``server/turn_sync.py``;
``server.chat`` re-exports the two old names, so ``server.chat.<name>`` still RESOLVES —
but it is a copy of the binding. A monkeypatch of it there intercepts nothing
(``_chat_langgraph`` calls ``_turn_sync._chat_langgraph_impl``). This scans the suite so a
stale target fails loudly: patch these names on ``server.turn_sync``.

The other direction is pinned too: the moved driver reaches every collaborator that
STAYED in ``server.chat`` through ``server.chat`` at CALL time — a source scan plus a
break-the-fake test — so a patch on ``server.chat`` still lands; and the wrapper reaches
the impl through ``server.turn_sync`` at call time.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

import server.turn_sync as turn_sync
from tests._seam_scan import stale_patches

# Every name that moved and is re-exported by ``server.chat`` as the same object.
_MOVED = ("_chat_langgraph_impl", "_trace_reply_output")
# Former closures, now module-level in ``server.turn_sync`` only (never existed on server.chat).
_LIFTED = ("_native_turn", "_last_ai")
# Collaborators that stayed in ``server.chat``: the moved driver reads each as
# ``_chat().<name>`` at call time, never by a bare (import-time) binding.
_CALL_THROUGH = (
    "_OVERFLOW_RETRY_PROMPT",
    "_fail_turn",
    "_interrupt_payload",
    "_last_tool_text",
    "_overflow_compacted",
    "_pending_interrupt_value",
    "_resume_payload",
    "_set_trace_output",
    "_vision_human_message",
    "this_turn_messages",
    "turn_error",
)

_SELF = Path(__file__).resolve()
_REPO = _SELF.parent.parent


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


def test_no_test_patches_moved_turn_sync_names_on_server_chat():
    stale = stale_patches("server.chat", _MOVED + _LIFTED, exclude=[_SELF])
    assert not stale, "patch these on server.turn_sync, not server.chat (#3917): " + ", ".join(stale)


def test_re_exports_are_the_same_objects():
    chat = _chat()
    for name in _MOVED:
        assert getattr(chat, name) is getattr(turn_sync, name), name
    for name in _LIFTED:
        assert not hasattr(chat, name), name  # lifted closures have one home


def test_the_turn_sync_module_imports_without_server_chat():
    """No import-time edge back into ``server.chat`` (it is reached at call time only)."""
    subprocess.run([sys.executable, "-c", "import server.turn_sync"], check=True, cwd=str(_REPO))
    tree = ast.parse(Path(turn_sync.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "server.chat" for n in top)
    assert not any(isinstance(n, ast.Import) and any(a.name == "server.chat" for a in n.names) for n in top)
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module == "server" and any(a.name == "chat" for a in n.names) for n in top
    )


def _is_chat_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_chat"


def test_moved_driver_reads_chat_collaborators_at_call_time():
    """In ``server/turn_sync.py`` every ``_CALL_THROUGH`` name is read only as
    ``_chat().<name>`` — never a bare global (an import-time copy a patch misses)."""
    offenders: list[str] = []
    tree = ast.parse(Path(turn_sync.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _CALL_THROUGH:
            offenders.append(f"turn_sync.py:{node.lineno} bare {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _CALL_THROUGH and not _is_chat_call(node.value):
            offenders.append(f"turn_sync.py:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "reach these through server.chat at call time (#3917): " + ", ".join(offenders)


def test_moved_driver_reaches_owned_collaborators_through_their_owner():
    """The names the driver used from turn_control / chat_dispatch / chat_acp / turn_stream /
    turn_telemetry are read as ``_<owner>.<name>`` in ``server/turn_sync.py`` (the scans in
    those modules' own seam guards list files explicitly and predate this module)."""
    from tests.test_turn_control_seam import _CALL_THROUGH as _TURN_CONTROL

    owners = {
        "_turn_control": set(_TURN_CONTROL),
        "_chat_dispatch": {"_PreTurn", "_pre_turn_dispatch", "_short_circuit_reply"},
        "_chat_acp": {"_acp_turn_collected"},
        "_turn_stream": {"_fence_update"},
        "_turn_telemetry": {"make_usage_callback", "sum_usage"},
    }
    offenders: list[str] = []
    tree = ast.parse(Path(turn_sync.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        for owner, names in owners.items():
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in names:
                offenders.append(f"turn_sync.py:{node.lineno} bare {node.id}")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in names
                and not (isinstance(node.value, ast.Name) and node.value.id == owner)
            ):
                offenders.append(f"turn_sync.py:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "reach these through their owning module (#3917): " + ", ".join(offenders)


def test_callers_reach_the_impl_through_turn_sync():
    """No caller in ``server/`` reads ``_chat_langgraph_impl`` by a bare binding — only as
    ``_turn_sync._chat_langgraph_impl``, so a patch on the owner is what runs."""
    offenders: list[str] = []
    for path in sorted((_REPO / "server").rglob("*.py")):
        if path.name == "turn_sync.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id == "_chat_langgraph_impl":
                offenders.append(f"{path.name}:{node.lineno} bare _chat_langgraph_impl")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr == "_chat_langgraph_impl"
                and not (isinstance(node.value, ast.Name) and node.value.id == "_turn_sync")
            ):
                offenders.append(f"{path.name}:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "call it as _turn_sync._chat_langgraph_impl (#3917): " + ", ".join(offenders)


# ── break-the-fake: patches land at call time ──────────────────────────────────


@pytest.fixture
def graph_env(monkeypatch):
    """The characterization suite's STATE baseline, a no-op trace, and a graph installer."""
    import runtime.state as rs
    from graph.config import LangGraphConfig
    from tests._turn_driver_fakes import ScriptedGraph, TraceSpy

    TraceSpy().install(monkeypatch)
    for attr, val in {
        "goal_controller": None,
        "background_mgr": None,
        "watch_controller": None,
        "scheduler": None,
        "graph_auth_error": None,
        "thread_id_resolver": None,
        "checkpointer": object(),
        "knowledge_store": None,
        "graph_config": LangGraphConfig(),
    }.items():
        monkeypatch.setattr(rs.STATE, attr, val, raising=False)

    def install(invokes=()):
        graph = ScriptedGraph(invokes=invokes)
        monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
        return graph

    return install


@pytest.mark.asyncio
async def test_wrapper_calls_the_patched_turn_sync_impl(monkeypatch):
    """A patch of ``server.turn_sync._chat_langgraph_impl`` is what ``_chat_langgraph`` runs."""
    calls: list = []

    async def _fake(message, session_id, **_kw):
        calls.append(message)
        return [{"role": "assistant", "content": "from the patched impl"}]

    monkeypatch.setattr(turn_sync, "_chat_langgraph_impl", _fake)
    out = await _chat()._chat_langgraph("go", "s-sync")
    assert calls == ["go"] and out == [{"role": "assistant", "content": "from the patched impl"}]


@pytest.mark.asyncio
async def test_moved_driver_uses_patched_chat_collaborators(monkeypatch, graph_env):
    """Each ``_CALL_THROUGH`` collaborator patched on ``server.chat`` is what the moved
    driver runs (break the fake → this goes red)."""
    from langchain_core.messages import AIMessage, HumanMessage

    from tests._turn_driver_fakes import Raise, set_interrupt, turn_result

    chat = _chat()
    seen: list[str] = []
    real_pending = chat._pending_interrupt_value

    def _rec(tag, ret):
        def _f(*a, **k):
            seen.append(tag)
            return ret

        return _f

    # 1) plain turn: vision message, this-turn scan, tool fallback, no-reply error, trace output.
    graph = graph_env([turn_result()])
    monkeypatch.setattr(chat, "_vision_human_message", _rec("vision", HumanMessage(content="V")))
    monkeypatch.setattr(chat, "this_turn_messages", _rec("this_turn", [AIMessage(content="PATCHED-AI")]))
    monkeypatch.setattr(chat, "_set_trace_output", _rec("trace", None))
    out = await turn_sync._chat_langgraph_impl("hi", "s-cc")
    assert graph.invoke_calls[0][0]["messages"][0].content == "V"
    assert out[0]["content"] == "PATCHED-AI"
    assert {"vision", "this_turn", "trace"} <= set(seen)

    # 2) no text → the patched tool fallback; then nothing at all → the patched turn_error.
    monkeypatch.setattr(chat, "this_turn_messages", _rec("this_turn", []))
    monkeypatch.setattr(chat, "_last_tool_text", _rec("tool_text", "PATCHED-TOOL"))
    graph_env([turn_result()])
    assert (await turn_sync._chat_langgraph_impl("hi", "s-cc"))[0]["content"] == "PATCHED-TOOL"
    monkeypatch.setattr(chat, "_last_tool_text", _rec("tool_text", ""))
    monkeypatch.setattr(chat, "turn_error", _rec("turn_error", {"type": "PATCHED-ERR"}))
    graph_env([turn_result()])
    assert (await turn_sync._chat_langgraph_impl("hi", "s-cc"))[0]["error"] == {"type": "PATCHED-ERR"}

    # 3) a turn that parks: the patched interrupt reader + payload shaper. The pre-turn
    #    HITL hold reads the reader first (turn_control, also through server.chat) — say
    #    "nothing pending" there, then report the park after the turn.
    answers = [None, "patched?"]

    async def _pending(config):
        seen.append("pending")
        return answers.pop(0)

    monkeypatch.setattr(chat, "_pending_interrupt_value", _pending)
    monkeypatch.setattr(chat, "_interrupt_payload", _rec("payload", {"question": "PATCHED-Q"}))
    graph_env([turn_result()])
    out = await turn_sync._chat_langgraph_impl("hi", "s-cc")
    assert out[0]["content"] == "🙋 **Input needed:** PATCHED-Q" and answers == []

    # 4) the HITL resume: the patched resume payload rides the Command.
    monkeypatch.setattr(chat, "_pending_interrupt_value", real_pending)
    monkeypatch.setattr(chat, "this_turn_messages", _rec("this_turn", [AIMessage(content="ok")]))

    async def _resume(config, value):
        seen.append("resume")
        return {"patched": value}

    monkeypatch.setattr(chat, "_resume_payload", _resume)
    graph = graph_env([turn_result()])
    set_interrupt({"question": "q"})(graph)
    await turn_sync._chat_langgraph_impl("ans", "s-cc", hitl_resume=True)
    assert graph.resumes == [{"patched": "ans"}] and "resume" in seen

    # 5) overflow: the patched classifier + recovery prompt; then a failure → patched _fail_turn.
    async def _compacted(exc, tid, sid):
        seen.append("compacted")
        return True

    monkeypatch.setattr(chat, "_overflow_compacted", _compacted)
    monkeypatch.setattr(chat, "_OVERFLOW_RETRY_PROMPT", "PATCHED-RETRY")
    monkeypatch.setattr(chat, "_vision_human_message", lambda m, *a, **k: HumanMessage(content=m))
    graph = graph_env([Raise(ValueError("boom")), turn_result()])
    await turn_sync._chat_langgraph_impl("big", "s-cc")
    assert graph.invoke_calls[1][0]["messages"][0].content == "PATCHED-RETRY" and "compacted" in seen

    async def _fail(exc, sid, *, tag, thread_id=None):
        seen.append("fail")
        return "PATCHED-FAIL"

    monkeypatch.setattr(chat, "_overflow_compacted", lambda *a: _false())
    monkeypatch.setattr(chat, "_fail_turn", _fail)
    graph_env([Raise(ValueError("boom"))])
    out = await turn_sync._chat_langgraph_impl("x", "s-cc")
    assert out[0]["content"] == "**Error:** PATCHED-FAIL" and "fail" in seen


async def _false():
    return False
