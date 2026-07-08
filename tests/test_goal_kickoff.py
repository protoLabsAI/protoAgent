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
        "s1", "cond", {"type": "command", "command": "pytest -q"},
        outcome="green CI", constraints=["do not touch prod"],
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


# ── #1911: declared-unattended + goal-autonomy in _is_autonomous ───────────
def test_is_autonomous_honors_unattended_and_origins():
    from server.chat import _is_autonomous

    # internal autonomous origins (unchanged)
    assert _is_autonomous({"origin": "scheduler"})
    assert _is_autonomous({"origin": "background-resume"})
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
