"""Goal kickoff + headless-first wiring (#1910 / #1911).

Freezes the two seams the live A2A path fell through:

  #1910 — the FIRST goal-driven turn now injects the goal condition (``kickoff_prompt``)
          so the agent begins on the goal instead of asking "what goal?", and a ``/goal``
          SET is distinguishable from status/clear (``is_set_ack``) so the runner knows to
          kick a drive turn rather than short-circuiting.
  #1911 — a plain A2A caller can DECLARE itself unattended (``_is_autonomous`` honors the
          ``unattended`` metadata flag), and a goal-driven turn is autonomous by definition.

Pure + hermetic: the controller helpers take deterministic inputs; ``_is_autonomous`` is a
plain function. No model, no graph, no network.
"""

from __future__ import annotations

from graph.config import LangGraphConfig
from graph.goals.controller import GoalController, SET_ACK_PREFIX
from graph.goals.store import GoalStore


def _ctrl(tmp_path, **overrides) -> GoalController:
    return GoalController(LangGraphConfig(**overrides), GoalStore(tmp_path))


# ── #1910: is_set_ack distinguishes a SET from status/clear ────────────────
def test_is_set_ack_only_true_for_successful_set(tmp_path):
    c = _ctrl(tmp_path)
    assert c.is_set_ack(f"{SET_ACK_PREFIX}goal [active] via llm: 'x' (iteration 0/8)")
    # status / clear / parse-error replies must NOT kick a drive turn
    assert not c.is_set_ack("No active goal for this session.")
    assert not c.is_set_ack("Goal cleared.")
    assert not c.is_set_ack("Could not parse goal. Use `/goal <text>` ...")
    assert not c.is_set_ack(None)
    assert not c.is_set_ack("")


async def test_parse_control_set_reply_is_a_set_ack(tmp_path):
    """The reply a real /goal SET produces must be recognized as a set-ack (round-trip)."""
    c = _ctrl(tmp_path)
    reply = await c.parse_control("/goal ship the thing", "s1", trusted=False)
    assert c.is_set_ack(reply)
    assert c.active_goal("s1") is not None
    # a status query on the same session is NOT a set-ack
    status = await c.parse_control("/goal", "s1", trusted=False)
    assert not c.is_set_ack(status)


# ── #1910: kickoff_prompt injects the goal condition ───────────────────────
def test_kickoff_prompt_carries_the_condition(tmp_path):
    c = _ctrl(tmp_path)
    ok, _ = c.set_goal_operator("s1", "make the tests pass", {"type": "llm"})
    assert ok
    prompt = c.kickoff_prompt(c.active_goal("s1"))
    assert "make the tests pass" in prompt
    assert "kickoff" in prompt.lower()
    # it must tell the agent to BEGIN, not to ask which goal
    assert "abandon_goal" in prompt


def test_kickoff_prompt_folds_in_user_message_but_not_a_goal_command(tmp_path):
    c = _ctrl(tmp_path)
    c.set_goal_operator("s1", "cond", {"type": "llm"})
    gs = c.active_goal("s1")
    # a plain inbound message rides along
    with_msg = c.kickoff_prompt(gs, user_message="also check the logs")
    assert "also check the logs" in with_msg
    # the raw /goal command must NOT be echoed back into the prompt
    cmd = c.kickoff_prompt(gs, user_message="/goal cond")
    assert "/goal cond" not in cmd


def test_kickoff_prompt_includes_contract_when_present(tmp_path):
    c = _ctrl(tmp_path)
    c.set_goal_operator(
        "s1",
        "cond",
        {"type": "command", "command": "pytest -q"},
        outcome="green CI",
        constraints=["do not touch prod"],
    )
    prompt = c.kickoff_prompt(c.active_goal("s1"))
    assert "green CI" in prompt
    assert "do not touch prod" in prompt


def test_kickoff_prompt_fresh_context_variant(tmp_path):
    c = _ctrl(tmp_path)
    c.set_goal_operator("s1", "cond", {"type": "llm"})
    gs = c.active_goal("s1")
    gs.fresh_context = True
    prompt = c.kickoff_prompt(gs)
    assert "fresh context" in prompt
    assert "update_goal_plan" in prompt


# ── #4833: kickoff names the verifier so an operator goal isn't abandoned iter 0 ──
def test_kickoff_names_the_verifier_without_a_contract(tmp_path):
    """A command-verifier goal with NO contract must still learn its verifier in the kickoff
    (both the normal and fresh-context branches), so the agent works toward the check instead
    of hunting the condition's wording and abandoning on iteration 0 (#4833)."""
    c = _ctrl(tmp_path)
    c.set_goal_operator("s1", "the readiness check passes", {"type": "command", "command": "false"})
    gs = c.active_goal("s1")
    assert not gs.has_contract  # precondition: no contract, so the contract block is absent
    for fresh in (False, True):
        gs.fresh_context = fresh
        prompt = c.kickoff_prompt(gs)
        assert "command: false" in prompt  # the verifier summary is present
        assert "decided by the verifier" in prompt


def test_kickoff_with_a_contract_does_not_state_the_verifier_twice(tmp_path):
    """When a goal HAS a contract, the contract block already names the verifier; the kickoff
    must not add a second, standalone verifier statement (#4833)."""
    c = _ctrl(tmp_path)
    c.set_goal_operator(
        "s1",
        "cond",
        {"type": "command", "command": "pytest -q"},
        outcome="green CI",
    )
    prompt = c.kickoff_prompt(c.active_goal("s1"))
    # the verifier summary appears exactly once — only inside the contract block
    assert prompt.count("command: pytest -q") == 1
    # and the standalone kickoff verifier line is suppressed
    assert "decided by the verifier" not in prompt


def test_kickoff_deterministic_verifier_says_missing_condition_is_not_abandon(tmp_path):
    """For a deterministic (command/test/ci/data/plugin) verifier, the kickoff tells the
    agent that failing to find the condition's wording in the workspace is not grounds to
    abandon the goal — while keeping abandon_goal for impossible/out-of-scope goals (#4833)."""
    c = _ctrl(tmp_path)
    c.set_goal_operator("s1", "the readiness check passes", {"type": "command", "command": "false"})
    gs = c.active_goal("s1")
    for fresh in (False, True):
        gs.fresh_context = fresh
        prompt = c.kickoff_prompt(gs)
        assert "abandon_goal" in prompt
        assert "not grounds" in prompt
        assert "workspace" in prompt


def test_kickoff_llm_verifier_gets_summary_line_but_not_the_deterministic_note(tmp_path):
    """The fuzzy llm verifier still gets the one-line verifier statement (so the agent knows a
    model judges DONE), but NOT the deterministic 'can't locate the condition' note, which
    would be noise for a judge that reads the turn's text (#4833)."""
    c = _ctrl(tmp_path)
    c.set_goal_operator("s1", "ship the thing", {"type": "llm"})
    prompt = c.kickoff_prompt(c.active_goal("s1"))
    assert "llm judgment" in prompt
    assert "decided by the verifier" in prompt
    assert "not grounds" not in prompt


# ── #1911: declared-unattended + goal-autonomy in _is_autonomous ───────────
def test_is_autonomous_honors_unattended_and_origins():
    from server.chat import _is_autonomous

    # internal autonomous origins (unchanged)
    assert _is_autonomous({"origin": "scheduler"})
    assert _is_autonomous({"origin": "background-resume"})
    assert _is_autonomous({"origin": "delegate-result"})
    # declared unattended — bool AND string forms (JSON-RPC may send either)
    assert _is_autonomous({"unattended": True})
    assert _is_autonomous({"unattended": "true"})
    assert _is_autonomous({"unattended": "1"})
    # NOT autonomous: plain operator-attended A2A, falsey/absent flags
    assert not _is_autonomous({"origin": "user"})
    assert not _is_autonomous({"unattended": "false"})
    assert not _is_autonomous({"unattended": False})
    assert not _is_autonomous({})
    assert not _is_autonomous(None)


# --- ADR 0079: goal drive references tasks/watches/scheduling ---------------
def test_kickoff_and_continuation_reference_composition(tmp_path):
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore
    from graph.goals.types import VerifyResult

    c = GoalController(LangGraphConfig(), GoalStore(tmp_path))
    c.set_goal_operator("s", "ship it", {"type": "llm"})
    gs = c.active_goal("s")
    kick = c.kickoff_prompt(gs)
    cont = c._continuation(gs, VerifyResult(met=False, reason="not yet", evidence=""))
    for prompt in (kick, cont):
        assert "task_create" in prompt
        assert "create_watch" in prompt or "schedule_task" in prompt
        assert "END the turn" in prompt  # the async-handoff / don't-spin directive
