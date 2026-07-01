"""GoalController — control parsing + decision matrix (goal mode)."""

import pytest

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController
from graph.goals.store import GoalStore


def _ctrl(tmp_path, **overrides):
    cfg = LangGraphConfig(**overrides)
    return GoalController(cfg, GoalStore(tmp_path))


# --- control parsing --------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_non_goal_returns_none(tmp_path):
    assert await _ctrl(tmp_path).parse_control("hello there", "s") is None


@pytest.mark.asyncio
async def test_parse_set_plain_text(tmp_path):
    c = _ctrl(tmp_path)
    reply = await c.parse_control("/goal make the build green", "s")
    assert "Goal set" in reply
    state = c.active_goal("s")
    assert state.condition == "make the build green"
    assert state.verifier["type"] == "llm"


@pytest.mark.asyncio
async def test_parse_set_json_spec(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control(
        '/goal {"condition": "tests pass", "verifier": {"type": "command", "command": "pytest -q"}, "max_iterations": 3}',
        "s",
    )
    state = c.active_goal("s")
    assert state.verifier == {"type": "command", "command": "pytest -q"}
    assert state.max_iterations == 3


@pytest.mark.asyncio
async def test_parse_status_and_clear(tmp_path):
    c = _ctrl(tmp_path)
    assert "No active goal" in await c.parse_control("/goal", "s")
    await c.parse_control("/goal do x", "s")
    assert "goal [active]" in await c.parse_control("/goal", "s")
    assert "cleared" in (await c.parse_control("/goal clear", "s")).lower()
    assert c.active_goal("s") is None


@pytest.mark.asyncio
async def test_parse_clear_aliases(tmp_path):
    c = _ctrl(tmp_path)
    for alias in ("stop", "off", "cancel", "reset", "none"):
        await c.parse_control("/goal do x", "s")
        await c.parse_control(f"/goal {alias}", "s")
        assert c.active_goal("s") is None


# --- evaluate ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_no_active_goal(tmp_path):
    assert await _ctrl(tmp_path).evaluate("s", last_text="x") is None


@pytest.mark.asyncio
async def test_evaluate_met(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 0"}}', "s")
    decision = await c.evaluate("s", last_text="all set")
    assert decision.action == "done"
    assert decision.state.status == "achieved"


@pytest.mark.asyncio
async def test_evaluate_not_met_continues(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    decision = await c.evaluate("s", last_text="working")
    assert decision.action == "continue"
    assert "NOT yet met" in decision.message
    assert c.active_goal("s").iteration == 1


@pytest.mark.asyncio
async def test_evaluate_exhausts_budget(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control(
        '/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}, "max_iterations": 1}',
        "s",
    )
    decision = await c.evaluate("s", last_text="working")
    assert decision.action == "done"
    assert decision.state.status == "exhausted"


@pytest.mark.asyncio
async def test_evaluate_no_progress_flags_unachievable(tmp_path):
    c = _ctrl(tmp_path, goal_no_progress_limit=2, goal_max_iterations=20)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    status = None
    for _ in range(6):
        decision = await c.evaluate("s", last_text="same output every time")
        if decision.action == "done":
            status = decision.state.status
            break
    assert status == "unachievable"


@pytest.mark.asyncio
async def test_model_giveup_flags_unachievable(tmp_path):
    # Verifier fails (exit 1), so the agent's give-up is honoured.
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    decision = await c.evaluate("s", last_text='cannot do this <goal_unachievable reason="needs prod access"/>')
    assert decision.action == "done"
    assert decision.state.status == "unachievable"
    assert "prod access" in decision.state.last_reason


@pytest.mark.asyncio
async def test_verifier_overrides_giveup(tmp_path):
    # Ground truth wins: when the verifier passes, a same-turn give-up tag
    # must NOT mask the achievement.
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 0"}}', "s")
    decision = await c.evaluate("s", last_text='giving up <goal_unachievable reason="cannot proceed"/>')
    assert decision.action == "done"
    assert decision.state.status == "achieved"


@pytest.mark.asyncio
async def test_checklist_extracted_and_carried(tmp_path):
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    decision = await c.evaluate("s", last_text="progress <goal_plan>1. do A\n2. do B</goal_plan> more")
    assert "do A" in c.active_goal("s").checklist
    assert "do A" in decision.message


# --- Phase 1 chat trust-gate (#1407) ---------------------------------------


@pytest.mark.asyncio
async def test_untrusted_chat_refuses_shell_and_eval_verifiers(tmp_path):
    # command/test/ci shell out; data+expr is a restricted-eval sink — all refused from a
    # chat message (trusted=False), and NOTHING is set.
    c = _ctrl(tmp_path)
    dangerous = [
        '/goal {"condition": "x", "verifier": {"type": "command", "command": "rm -rf /"}}',
        '/goal {"condition": "x", "verifier": {"type": "test", "command": "pytest"}}',
        '/goal {"condition": "x", "verifier": {"type": "ci", "pr": 1}}',
        '/goal {"condition": "x", "verifier": {"type": "data", "path": "/x", "expr": "1"}}',
    ]
    for msg in dangerous:
        reply = await c.parse_control(msg, "s", trusted=False)
        assert "can't be" in reply.lower(), msg
        assert c.active_goal("s") is None, msg


@pytest.mark.asyncio
async def test_untrusted_chat_allows_declarative_verifiers(tmp_path):
    c = _ctrl(tmp_path)
    ok = [
        "/goal make the build green",  # fuzzy → llm
        '/goal {"condition": "x", "verifier": {"type": "plugin", "check": "p:v"}}',
        '/goal {"condition": "x", "verifier": {"type": "data", "path": "/x", "contains": "ok"}}',
    ]
    for msg in ok:
        reply = await c.parse_control(msg, "s", trusted=False)
        assert "Goal set" in reply, msg
        c._store.clear("s")


@pytest.mark.asyncio
async def test_trusted_default_keeps_full_verifier_access(tmp_path):
    # The operator/programmatic path (trusted=True, the default) is unchanged — Phase 2
    # threads a real trust signal into the chat call sites.
    c = _ctrl(tmp_path)
    reply = await c.parse_control(
        '/goal {"condition": "x", "verifier": {"type": "command", "command": "exit 0"}}', "s"
    )
    assert "Goal set" in reply
    assert c.active_goal("s").verifier["type"] == "command"


def test_chat_verifier_allow_list():
    allowed = GoalController._chat_verifier_allowed
    assert allowed({"type": "plugin", "check": "p:v"})
    assert allowed({"type": "llm"})
    assert allowed({})  # no type → defaults to llm
    assert allowed({"type": "data", "contains": "ok"})
    assert not allowed({"type": "data", "expr": "1"})
    assert not allowed({"type": "data", "contains": "ok", "expr": "1"})  # expr present → refused
    assert not allowed({"type": "command", "command": "x"})
    assert not allowed({"type": "test"})
    assert not allowed({"type": "ci"})
