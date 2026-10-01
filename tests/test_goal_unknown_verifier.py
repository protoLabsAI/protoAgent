"""An unknown plugin verifier is refused on EVERY goal-set entry point (#3946).

A goal whose ``check`` names a verifier the live registry doesn't know is created but can
never pass — it spins to the iteration cap and ends 'unachievable'. The agent's ``set_goal``
tool already refused it; ``POST /api/goals`` (operator), ``set_goal_safe`` (plugin SDK /
programmatic) and the chat ``/goal`` path did not, and the REST route even kicked a turn.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.goals.controller import GoalController
from graph.goals.store import GoalStore

_KNOWN = "demo:probe"
_UNKNOWN = {"type": "plugin", "check": "nope:nope"}


@pytest.fixture
def ctrl(tmp_path, monkeypatch):
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {_KNOWN: object()})
    return GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))


def test_set_goal_safe_rejects_unknown_plugin_verifier(ctrl):
    ok, msg = ctrl.set_goal_safe("s", "cond", dict(_UNKNOWN))
    assert ok is False
    assert "unknown plugin verifier 'nope:nope'" in msg and _KNOWN in msg
    assert ctrl.active_goal("s") is None


def test_set_goal_safe_accepts_registered_plugin_verifier(ctrl):
    ok, _ = ctrl.set_goal_safe("s", "cond", {"type": "plugin", "check": _KNOWN})
    assert ok is True and ctrl.active_goal("s") is not None


def test_set_goal_operator_rejects_unknown_plugin_verifier(ctrl):
    ok, msg = ctrl.set_goal_operator("s", "cond", dict(_UNKNOWN))
    assert ok is False and "unknown plugin verifier" in msg
    assert ctrl.active_goal("s") is None


def test_set_goal_operator_rejects_plugin_verifier_without_check(ctrl):
    ok, msg = ctrl.set_goal_operator("s", "cond", {"type": "plugin"})
    assert ok is False and "check" in msg
    assert ctrl.active_goal("s") is None


def test_set_goal_operator_non_plugin_verifiers_unaffected(ctrl):
    ok, _ = ctrl.set_goal_operator("s", "cond", {"type": "llm"})
    assert ok is True


def test_set_goal_safe_reports_empty_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("graph.goals.verifiers._PLUGIN_VERIFIERS", {})
    c = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    ok, msg = c.set_goal_safe("s", "cond", dict(_UNKNOWN))
    assert ok is False and "none registered" in msg


@pytest.mark.asyncio
async def test_chat_goal_rejects_unknown_plugin_verifier(ctrl):
    reply = await ctrl.parse_control('/goal {"condition": "x", "verifier": {"type": "plugin", "check": "nope:nope"}}', "s", trusted=False)
    assert "unknown plugin verifier" in reply
    assert not GoalController.is_set_ack(reply)  # so the chat runner does NOT kick a turn
    assert ctrl.active_goal("s") is None


def test_post_api_goals_unknown_verifier_is_400_and_not_kicked(ctrl, monkeypatch):
    from operator_api import console_handlers
    from operator_api.routes import register_operator_routes
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "goal_controller", ctrl)
    kicks: list[str] = []
    monkeypatch.setattr(console_handlers, "_safe_kick", lambda sid, prompt: kicks.append(sid) or True)

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        goal_set=console_handlers._operator_goals_set,
    )
    client = TestClient(app)

    bad = client.post("/api/goals", json={"session_id": "s", "condition": "x", "verifier": dict(_UNKNOWN)})
    assert bad.status_code == 400
    assert "unknown plugin verifier 'nope:nope'" in bad.json()["detail"]
    assert kicks == [] and ctrl.active_goal("s") is None

    good = client.post("/api/goals", json={"session_id": "s", "condition": "x", "verifier": {"type": "plugin", "check": _KNOWN}})
    assert good.status_code == 200 and good.json()["kicked"] is True
    assert kicks == ["s"]


@pytest.mark.asyncio
async def test_chat_goal_unknown_verifier_type_says_so_not_safety_refusal(ctrl):
    """#3957: an unknown verifier TYPE from chat used to hit the trust-gate first and come
    back as the "For safety, a command/test/ci…" refusal — misleading for a typo."""
    reply = await ctrl.parse_control('/goal {"condition": "x", "verifier": {"type": "comand", "command": "true"}}', "s", trusted=False)
    assert "unknown verifier type 'comand'" in reply
    assert "For safety" not in reply
    assert not GoalController.is_set_ack(reply)
    assert ctrl.active_goal("s") is None


@pytest.mark.asyncio
async def test_chat_goal_known_unsafe_type_still_hits_trust_gate(ctrl):
    reply = await ctrl.parse_control('/goal {"condition": "x", "verifier": {"type": "command", "command": "true"}}', "s", trusted=False)
    assert "For safety" in reply
    assert ctrl.active_goal("s") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("vtype", ['["command"]', '{"x": 1}', "7", "null"])
async def test_chat_goal_non_string_verifier_type_is_refused_not_a_crash(ctrl, vtype):
    """CodeRabbit (controller): a JSON verifier may carry any value as its type; a list
    is unhashable, so the membership check raised TypeError instead of refusing."""
    reply = await ctrl.parse_control(f'/goal {{"condition": "x", "verifier": {{"type": {vtype}}}}}', "s", trusted=False)
    assert "unknown verifier type" in reply
    assert ctrl.active_goal("s") is None
