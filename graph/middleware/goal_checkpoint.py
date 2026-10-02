"""Detect that a goal-driven turn already met its goal, so it can end there.

The goal drive (``server/goal_loop.py``) verifies the outcome only after the agent STOPS.
Inside one turn nothing checked: a model that fixed the bug and saw the tests pass still
had a ``<working_state>`` reading ``GOAL [active]`` and its own plan, so it kept going —
re-exploring the repo, re-running the tests, and telling the operator "the goal is
already complete" two or three times before it finally ended with a text-only reply. In
the console that reads as several runs stacked into one bubble.

The agent's checkpoint is ``update_goal_plan``: the kickoff and continuation prompts ask
it to record its plan as it works, and it does so when it believes it has made progress.
So on a goal-driven turn (``graph.goals.goal_turn``), right after a successful
``update_goal_plan`` call, :func:`goal_met_after_plan` runs the goal's verifier as a
side-effect-free probe (``GoalController.probe`` — mechanical verifier types only, never
``llm``). If it passes, ``WaitYieldMiddleware`` ends the turn there (``jump_to: end``, the
same "a tool round ended the turn" exit as ``wait``) — hosted there rather than in a
middleware of its own because every ``before_model`` hook is one more graph node per
round, i.e. one more step of every turn's ``recursion_limit``. The turn's streamed text
is the reply, and the post-turn ``evaluate`` records the outcome and shows "✓ goal
achieved". A failing probe changes nothing: the agent keeps working.

False on a non-goal turn, on a turn that didn't just record its plan, and with no goal
controller. Async only (the verifier is async; the server always drives the graph async).
"""

from __future__ import annotations

import logging

from langchain_core.messages import ToolMessage

from graph.goals.goal_turn import in_goal_turn, record_goal_met

log = logging.getLogger(__name__)

PLAN_TOOL_NAME = "update_goal_plan"


def just_recorded_plan(messages: list) -> bool:
    """True if the trailing tool-result block holds a SUCCESSFUL ``update_goal_plan``
    result — the agent recorded its plan in the round that just ran."""
    for m in reversed(messages or []):
        if not isinstance(m, ToolMessage):
            break  # end of the trailing tool block
        if (getattr(m, "name", None) or "") != PLAN_TOOL_NAME or getattr(m, "status", None) == "error":
            continue
        content = m.content if isinstance(m.content, str) else ""
        if content.strip().lower().startswith("plan recorded"):
            return True
    return False


def _session_id(state) -> str:
    sid = ""
    if isinstance(state, dict):
        sid = str(state.get("session_id") or "").strip()
    if not sid:
        try:
            from observability import tracing

            sid = tracing.current_session_id() or ""
        except Exception:  # noqa: BLE001
            sid = ""
    return sid


async def goal_met_after_plan(state) -> bool:
    """On a goal-driven turn whose last round recorded the plan, probe the goal's verifier;
    True when it passes (and the pass's goal marker records why). Never raises."""
    if not in_goal_turn() or not just_recorded_plan((state or {}).get("messages") or []):
        return False
    from runtime.state import STATE

    ctrl = STATE.goal_controller
    sid = _session_id(state)
    if ctrl is None or not sid or not hasattr(ctrl, "probe"):
        return False
    result = await ctrl.probe(sid)
    if result is None or not result.met:
        return False
    log.info("[goal] verifier passed mid-turn for %s (%s) — ending the turn", sid, result.reason)
    record_goal_met(result.reason or "verifier passed")
    return True
