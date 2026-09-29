"""Seam guard for the shared goal loop + HITL auto-answer (#3884, epic #3804 slice 3).

``server/goal_loop.py`` is the ONE implementation of the goal drive (kickoff, verify /
continue, fresh-context config, hard cap, async-handoff pause, terminal note) and the
autonomous HITL auto-answer policy (park / answer / give up, id-keyed resume) — used by
BOTH turn drivers in ``server/chat.py``. Pinned here:

* ``server.chat`` does NOT re-export the new names (so there is no copy a test could patch
  by mistake), the drivers reach them only as ``_goal_loop.<name>`` at call time, and no
  test patches them on ``server.chat`` (``tests/_seam_scan.py``).
* The collaborators that STAYED in ``server.chat`` (the async-handoff check, the
  continuation config, the interrupt readers / resume builder / clear) are read as
  ``_chat().<name>`` at call time — a source scan plus a break-the-fake test on each
  driver — and ``goal_loop`` has no import-time edge back into ``server.chat``.
* Both drivers really run the shared code: a patch on ``server.goal_loop`` changes what
  the streaming AND the non-streaming driver do.
"""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

import server.goal_loop as goal_loop
import server.turn_control as turn_control
from graph.config import LangGraphConfig
from tests._seam_scan import stale_patches
from tests._turn_driver_fakes import FakeGoals, Invoke, ScriptedGraph, set_interrupt, text, turn_result

_SHARED = (
    "ANSWER",
    "GIVE_UP",
    "GoalContinuation",
    "GoalDrive",
    "GoalNote",
    "HitlAutoAnswer",
    "PARK",
    "PAUSE_NOTE",
    "active_goal",
    "is_autonomous_turn",
    "kickoff_message",
)
# Collaborators that stay in ``server.chat`` — goal_loop reads each as ``_chat().<name>``.
_CALL_THROUGH = (
    "_awaiting_self_resume",
    "_clear_pending_interrupt",
    "_goal_continuation_config",
    "_pending_interrupt_value",
    "_resume_payload",
)

_SELF = Path(__file__).resolve()
_REPO = _SELF.parent.parent


def _chat():
    # By path: ``server`` re-exports the ``chat`` FUNCTION under the submodule's name.
    return importlib.import_module("server.chat")


# ── source scans ──────────────────────────────────────────────────────────────


def test_shared_names_are_not_re_exported_from_server_chat():
    chat = _chat()
    for name in _SHARED:
        assert hasattr(goal_loop, name), name
        assert not hasattr(chat, name), f"server.chat must not copy {name} — call _goal_loop.{name}"


def test_no_test_patches_goal_loop_names_on_server_chat():
    stale = stale_patches("server.chat", _SHARED, exclude=[_SELF])
    assert not stale, "patch these on server.goal_loop (#3884): " + ", ".join(stale)


def test_goal_loop_imports_without_server_chat():
    subprocess.run([sys.executable, "-c", "import server.goal_loop"], check=True, cwd=str(_REPO))
    tree = ast.parse(Path(goal_loop.__file__).read_text(encoding="utf-8"))
    top = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(isinstance(n, ast.ImportFrom) and n.module == "server.chat" for n in top)
    assert not any(isinstance(n, ast.Import) and any(a.name == "server.chat" for a in n.names) for n in top)
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module == "server" and any(a.name == "chat" for a in n.names) for n in top
    )


def test_goal_loop_reads_chat_collaborators_at_call_time():
    offenders: list[str] = []
    tree = ast.parse(Path(goal_loop.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _CALL_THROUGH:
            offenders.append(f"goal_loop.py:{node.lineno} bare {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr in _CALL_THROUGH:
            v = node.value
            direct = isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == "_chat"
            # ``chat = _chat()`` bound once inside a function, then ``chat.<name>``.
            local = isinstance(v, ast.Name) and v.id == "chat"
            if not (direct or local):
                offenders.append(f"goal_loop.py:{node.lineno} {ast.unparse(node)}")
    assert not offenders, "reach these through server.chat at call time (#3884): " + ", ".join(offenders)


def test_drivers_reach_the_shared_loop_through_the_module():
    """No module in ``server/`` binds a goal_loop name directly (``from server.goal_loop
    import …``) or reads one bare — only ``_goal_loop.<name>``, so a patch on the owner runs."""
    offenders: list[str] = []
    for path in sorted((_REPO / "server").rglob("*.py")):
        if path.name == "goal_loop.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "server.goal_loop":
                offenders.append(f"{path.name}:{node.lineno} from server.goal_loop import …")
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in _SHARED:
                offenders.append(f"{path.name}:{node.lineno} bare {node.id}")
    assert not offenders, "call these as _goal_loop.<name> (#3884): " + ", ".join(offenders)


# ── break-the-fake: both drivers run the shared code ────────────────────────────


@pytest.fixture
def state(monkeypatch):
    import runtime.state as rs

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
    return rs.STATE


async def _stream(message="go", sid="s1"):
    return [f async for f in _chat()._chat_langgraph_stream(message, sid, request_metadata={})]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_both_drivers_run_the_shared_goal_drive(state, monkeypatch, surface):
    """A patched ``goal_loop.GoalDrive`` is what either driver drives with."""
    built: list[str] = []

    class _Drive(goal_loop.GoalDrive):
        def __init__(self, session_id, config, text):
            built.append(text)
            super().__init__(session_id, config, text)

    monkeypatch.setattr(goal_loop, "GoalDrive", _Drive)
    monkeypatch.setattr(state, "goal_controller", FakeGoals([("done", "met")], iteration=1), raising=False)
    if surface == "stream":
        g = ScriptedGraph(streams=[[text("r1", "answer")]])
        monkeypatch.setattr(state, "graph", g, raising=False)
        assert (await _stream())[-1] == ("done", "answer\n\n---\nmet")
    else:
        g = ScriptedGraph(invokes=[turn_result(AIMessage(content="answer"))])
        monkeypatch.setattr(state, "graph", g, raising=False)
        assert (await _chat().chat("go", "s1"))[0]["content"] == "answer\n\n---\nmet"
    assert built == ["answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_both_drivers_run_the_shared_kickoff(state, monkeypatch, surface):
    seen: list = []

    def _kickoff(goal_state, message, *, resume):
        seen.append((message, resume))
        return f"KICK[{message}]"

    monkeypatch.setattr(goal_loop, "kickoff_message", _kickoff)
    monkeypatch.setattr(state, "goal_controller", FakeGoals([None]), raising=False)
    if surface == "stream":
        g = ScriptedGraph(streams=[[text("r1", "ok")]])
        monkeypatch.setattr(state, "graph", g, raising=False)
        await _stream()
        assert g.stream_calls[0][0]["messages"][-1].content == "KICK[go]"
    else:
        g = ScriptedGraph(invokes=[turn_result(AIMessage(content="ok"))])
        monkeypatch.setattr(state, "graph", g, raising=False)
        await _chat().chat("go", "s1")
        assert g.invoke_calls[0][0]["messages"][-1].content == "KICK[go]"
    assert seen == [("go", False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_both_drivers_run_the_shared_auto_answer_policy(state, monkeypatch, surface):
    """A patched ``HitlAutoAnswer`` (here: gives up at once) governs either driver."""
    cleared: list = []

    class _GiveUpNow(goal_loop.HitlAutoAnswer):
        def on_interrupt(self):
            return goal_loop.GIVE_UP

        async def give_up(self, config):
            cleared.append(config["configurable"]["thread_id"])

    monkeypatch.setattr(goal_loop, "HitlAutoAnswer", _GiveUpNow)
    monkeypatch.setattr(state, "goal_controller", FakeGoals([None]), raising=False)
    if surface == "stream":
        g = ScriptedGraph(streams=[[set_interrupt("q?")]])
        monkeypatch.setattr(state, "graph", g, raising=False)
        await _stream()
    else:
        g = ScriptedGraph(invokes=[Invoke(turn_result(), steps=[set_interrupt("q?")])])
        monkeypatch.setattr(state, "graph", g, raising=False)
        await _chat().chat("go", "s1")
    assert g.resumes == []
    assert cleared == ["a2a:s1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_shared_loop_reads_chat_collaborators_at_call_time(state, monkeypatch, surface):
    """Patches on ``server.chat`` (async handoff, continuation config, resume builder) land."""
    chat = _chat()
    seen: list[str] = []

    def _awaiting(sid):
        seen.append("awaiting")
        return len([s for s in seen if s == "awaiting"]) > 1  # continue once, then pause

    def _cont_config(config, goal_state):
        seen.append("cont")
        return {**config, "configurable": {"thread_id": "patched-cont"}}

    async def _resume(config, value):
        seen.append("resume")
        return {"patched": value}

    monkeypatch.setattr(chat, "_awaiting_self_resume", _awaiting)
    monkeypatch.setattr(chat, "_goal_continuation_config", _cont_config)
    monkeypatch.setattr(chat, "_resume_payload", _resume)
    monkeypatch.setattr(
        state, "goal_controller", FakeGoals([("continue", "n1", "more"), ("continue", "n2", "more")]), raising=False
    )
    if surface == "stream":
        g = ScriptedGraph(streams=[[set_interrupt("q?")], [text("r1", "a")], [text("r2", "b")]])
        monkeypatch.setattr(state, "graph", g, raising=False)
        frames = await _stream()
        assert frames[-1] == ("done", f"b\n\n---\n{goal_loop.PAUSE_NOTE}")
        assert g.stream_calls[2][1]["configurable"] == {"thread_id": "patched-cont"}
    else:
        g = ScriptedGraph(
            invokes=[
                Invoke(turn_result(), steps=[set_interrupt("q?")]),
                turn_result(AIMessage(content="a")),
                turn_result(AIMessage(content="b")),
            ]
        )
        monkeypatch.setattr(state, "graph", g, raising=False)
        out = await chat.chat("go", "s1")
        assert out[0]["content"] == f"b\n\n---\n{goal_loop.PAUSE_NOTE}"
        assert g.invoke_calls[2][1]["configurable"] == {"thread_id": "patched-cont"}
    assert g.resumes == [{"patched": turn_control._AUTONOMOUS_HITL_SENTINEL}]
    assert seen == ["resume", "awaiting", "cont", "awaiting"]


# ── behaviour the characterization suites didn't pin (found by the #3884 mutation check) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["stream", "sync"])
async def test_every_goal_driven_graph_call_runs_inside_goal_turn(state, monkeypatch, surface):
    """The initial turn AND each continuation run under ``goal_turn()`` (no cross-session
    ``<prior_sessions>`` injection into a goal drive) — on both drivers."""
    from graph.goals.goal_turn import in_goal_turn

    monkeypatch.setattr(
        state, "goal_controller", FakeGoals([("continue", "n", "more"), ("done", "d")], iteration=1), raising=False
    )
    seen: list[bool] = []
    if surface == "stream":
        g = ScriptedGraph(streams=[[text("r1", "a")], [text("r2", "b")]])
    else:
        g = ScriptedGraph(invokes=[turn_result(AIMessage(content="a")), turn_result(AIMessage(content="b"))])
    g.on_call = lambda graph, config: seen.append(in_goal_turn())
    monkeypatch.setattr(state, "graph", g, raising=False)
    if surface == "stream":
        await _stream()
    else:
        await _chat().chat("go", "s1")
    assert seen == [True, True]
    assert not in_goal_turn()


def test_auto_answer_policy_spends_its_budget_then_gives_up(monkeypatch):
    monkeypatch.setattr(turn_control, "_MAX_AUTONOMOUS_AUTOANSWERS", 2)
    aa = goal_loop.HitlAutoAnswer(True)
    verdicts = []
    for _ in range(3):
        verdicts.append(aa.on_interrupt())
        if verdicts[-1] == goal_loop.ANSWER:
            assert aa.answer() is turn_control._AUTONOMOUS_HITL_SENTINEL
    assert verdicts == [goal_loop.ANSWER, goal_loop.ANSWER, goal_loop.GIVE_UP]
    assert aa.answered == 2
    assert goal_loop.HitlAutoAnswer(False).on_interrupt() == goal_loop.PARK


@pytest.mark.parametrize(
    ("md", "goal_active", "expected"),
    [({}, False, False), ({}, True, True), ({"origin": "scheduler"}, False, True), (None, False, False)],
)
def test_is_autonomous_turn(md, goal_active, expected):
    assert goal_loop.is_autonomous_turn(md, goal_active=goal_active) is expected
