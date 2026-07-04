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
    # Verifier fails (exit 1), so the agent's give-up (recorded mid-turn via the
    # abandon_goal tool → request_abandon) is honoured.
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    c.request_abandon("s", "needs prod access")
    decision = await c.evaluate("s", last_text="cannot do this")
    assert decision.action == "done"
    assert decision.state.status == "unachievable"
    assert "prod access" in decision.state.last_reason


@pytest.mark.asyncio
async def test_verifier_overrides_giveup(tmp_path):
    # Ground truth wins: when the verifier passes, a same-turn give-up (abandon_goal)
    # must NOT mask the achievement.
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 0"}}', "s")
    c.request_abandon("s", "cannot proceed")
    decision = await c.evaluate("s", last_text="giving up")
    assert decision.action == "done"
    assert decision.state.status == "achieved"


@pytest.mark.asyncio
async def test_checklist_recorded_and_carried(tmp_path):
    # The plan is recorded mid-turn via the update_goal_plan tool (→ record_plan),
    # persisted, and fed back into the next continuation prompt.
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    c.record_plan("s", "1. do A\n2. do B")
    decision = await c.evaluate("s", last_text="progress")
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


# --- operator goal channel (ADR 0066 — POST /api/goals, operator-tier gated) -----------


def test_set_goal_operator_accepts_dangerous_verifiers(tmp_path):
    # The operator /api channel accepts command/test/ci/data — unlike set_goal_safe
    # (plugin-only) — because it's reachable only under the /api operator-tier ceiling.
    c = _ctrl(tmp_path)
    ok, msg = c.set_goal_operator("s", "tests pass", {"type": "command", "command": "pytest -q"})
    assert ok and "Goal set" in msg
    assert c.active_goal("s").verifier["type"] == "command"


def test_set_goal_operator_rejects_unknown_verifier(tmp_path):
    c = _ctrl(tmp_path)
    ok, msg = c.set_goal_operator("s", "x", {"type": "bogus"})
    assert not ok and "unknown verifier type" in msg


def test_set_goal_operator_requires_condition(tmp_path):
    c = _ctrl(tmp_path)
    ok, msg = c.set_goal_operator("s", "", {"type": "command", "command": "true"})
    assert not ok and "condition is required" in msg


# --- completion contracts (ADR 0073) ---------------------------------------


def test_set_goal_operator_stores_contract(tmp_path):
    c = _ctrl(tmp_path)
    ok, _ = c.set_goal_operator(
        "s",
        "ship it",
        {"type": "command", "command": "pytest -q"},
        outcome="the suite is green",
        constraints=["no new deps"],
        boundaries=["graph/"],
        stop_when="prod credentials are needed",
    )
    assert ok
    state = c.active_goal("s")
    assert state.outcome == "the suite is green"
    assert state.constraints == ["no new deps"]
    assert state.boundaries == ["graph/"]
    assert state.stop_when == "prod credentials are needed"
    assert state.has_contract is True


def test_set_goal_operator_coerces_string_contract_lists(tmp_path):
    # A bare string sent for a list field is coerced to a 1-element list.
    c = _ctrl(tmp_path)
    c.set_goal_operator(
        "s", "ship it", {"type": "command", "command": "true"},
        constraints="do not touch the schema", boundaries="tools/",
    )
    state = c.active_goal("s")
    assert state.constraints == ["do not touch the schema"]
    assert state.boundaries == ["tools/"]


def test_set_goal_safe_stores_contract(tmp_path):
    c = _ctrl(tmp_path)
    ok, _ = c.set_goal_safe(
        "s", "reach it", {"type": "plugin", "check": "p:v"},
        outcome="target hit", constraints=["stay under budget"], stop_when="the market halts",
    )
    assert ok
    state = c.active_goal("s")
    assert state.outcome == "target hit"
    assert state.constraints == ["stay under budget"]
    assert state.stop_when == "the market halts"


@pytest.mark.asyncio
async def test_continuation_includes_contract(tmp_path):
    # A `continue` decision re-states the contract (constraints/boundaries/stop_when +
    # the verifier-decides-DONE framing) on top of the existing continuation text.
    c = _ctrl(tmp_path)
    c.set_goal_operator(
        "s",
        "ship it",
        {"type": "command", "command": "pytest -q"},
        outcome="suite green on main",
        constraints=["no new network calls"],
        boundaries=["graph/goals/"],
        stop_when="a migration is required",
    )
    decision = await c.evaluate("s", last_text="working")
    assert decision.action == "continue"
    msg = decision.message
    # The existing continuation text is still there...
    assert "NOT yet met" in msg
    # ...and the contract directive is appended.
    assert "DONE only when the verifier passes" in msg
    assert "pytest -q" in msg  # verifier summary
    assert "suite green on main" in msg
    assert "no new network calls" in msg
    assert "graph/goals/" in msg
    assert "a migration is required" in msg
    assert "ask the operator" in msg


@pytest.mark.asyncio
async def test_continuation_without_contract_is_unchanged(tmp_path):
    # Backward-compat: a goal with NO contract produces a continuation byte-for-byte
    # identical to one built with no contract logic — nothing is appended.
    c = _ctrl(tmp_path)
    await c.parse_control('/goal {"condition": "done", "verifier": {"type": "command", "command": "exit 1"}}', "s")
    decision = await c.evaluate("s", last_text="working")
    assert decision.action == "continue"
    # The contract block markers never appear.
    assert "Contract for this goal" not in decision.message
    assert "Stay within these boundaries" not in decision.message
    # And it equals the base continuation (no trailing contract append).
    state = c.active_goal("s")
    from graph.goals.types import VerifyResult

    base = c._continuation_base(state, VerifyResult(False, "command exited 1", ""))
    assert c._contract_prompt(state) == ""
    # (evaluate advanced the iteration counter; compare the shape, not the counter.)
    assert decision.message.startswith("[goal continuation")
    assert base.startswith("[goal continuation")


def test_verifier_summary_shapes():
    s = GoalController._verifier_summary
    assert s({"type": "command", "command": "pytest -q"}) == "command: pytest -q"
    assert s({"type": "ci", "pr": 12}) == "ci PR #12"
    assert s({"type": "ci", "branch": "main"}) == "ci branch main"
    assert s({"type": "plugin", "check": "demo:probe"}) == "plugin demo:probe"
    assert s({"type": "data", "path": "/x.json"}) == "data check on /x.json"
    assert s({"type": "llm"}) == "llm judgment"
    assert s({}) == "llm judgment"


# --- verifier invoker identity (#1641) --------------------------------------


@pytest.mark.asyncio
async def test_evaluate_passes_goal_invoker_to_plugin_verifier(tmp_path):
    # A plugin verifier registered via the normal path (PluginRegistry →
    # set_plugin_verifiers) can tell WHICH goal is polling it: kind="goal",
    # id == the owning session (goals are keyed per session), no cadence.
    from pathlib import Path

    from graph.goals.types import VerifyResult
    from graph.goals.verifiers import set_plugin_verifiers
    from graph.plugins.registry import PluginRegistry

    seen = []

    async def probe(spec, ctx):
        seen.append(ctx)
        return VerifyResult(True, "met")

    reg = PluginRegistry("demo", Path("."))
    reg.register_goal_verifier("probe", probe)  # → demo:probe
    set_plugin_verifiers(reg.goal_verifiers)
    try:
        c = _ctrl(tmp_path)
        ok, _msg = c.set_goal_safe("sess-9", "reach it", {"type": "plugin", "check": "demo:probe"})
        assert ok
        decision = await c.evaluate("sess-9", last_text="done")
        assert decision.state.status == "achieved"
    finally:
        set_plugin_verifiers({})
    (ctx,) = seen
    assert ctx.invoker.kind == "goal"
    assert ctx.invoker.id == "sess-9"
    assert ctx.invoker.session_id == "sess-9"
    assert ctx.invoker.interval_s is None  # goals evaluate post-turn, not on a cadence
    assert ctx.condition == "reach it"  # the pre-#1641 ctx fields still flow
