"""Detect that a goal-driven turn already met its goal, so it can end there.

The goal drive (``server/goal_loop.py``) verifies the outcome only after the agent STOPS.
Inside one turn nothing checked: a model that fixed the bug and saw the tests pass still
had a ``<working_state>`` reading ``GOAL [active]``, so it kept going — re-exploring the
repo, re-running the tests, and telling the operator "the goal is already complete" two or
three times before it finally ended with a text-only reply. In the console that reads as
several runs stacked into one bubble.

So on a goal-driven turn (``graph.goals.goal_turn``), before the model is called again
after ANY tool round, :func:`goal_met_after_tools` runs the goal's verifier as a
side-effect-free probe (``GoalController.probe`` — mechanical verifier types only, never
``llm``). It probes after every tool round, not only after a write: there is no reliable
read/write classification across plugin, MCP and delegate tools, and the trigger must not
depend on which tool the model happened to call (an earlier version probed only after
``update_goal_plan`` and missed every turn that never called it). If the probe passes,
``WaitYieldMiddleware`` ends the turn there (``jump_to: end``, the same "a tool round ended
the turn" exit as ``wait``) — hosted there rather than in a middleware of its own because
every ``before_model`` hook is one more graph node per round, i.e. one more step of every
turn's ``recursion_limit``. The post-turn ``evaluate`` still decides and records the
outcome. A failing probe changes nothing: the agent keeps working.

**Debounced**, because a verifier can be slow (``goal_verify_timeout``, 120s by default)
and the turn sends nothing while it runs: at most one probe per round, and after a probe
none until ``max(PROBE_MIN_INTERVAL_S, 2 x its duration)`` has passed — a 1s test suite
is re-checked at most every 10s, a 60s one at most every 2 minutes. The bookkeeping rides
the pass's :class:`~graph.goals.goal_turn.GoalTurn` marker, so it is per pass. While a
probe runs, a ``goal_probe`` custom event gives the stream a transient status line.

False on a non-goal turn, after a model round that called no tools, with no goal
controller, and while debounced. Async only (the verifier is async; the server always
drives the graph async).
"""

from __future__ import annotations

import logging
import time
import uuid

from langchain_core.messages import AIMessage, ToolMessage

from graph.goals.goal_turn import current_goal_turn
from graph.middleware.guard_notes import guard_note

log = logging.getLogger(__name__)

# The gap between two mid-turn probes of one pass is at least PROBE_BACKOFF_FACTOR x the
# last probe's duration, and never under PROBE_MIN_INTERVAL_S seconds: cost-proportional,
# so a ~1s test suite is re-checked nearly every round (a fixed 10s floor let a fast fix
# slip past and the run-on through), while a slow verifier runs at most about once per
# three of its own durations (the probe, then twice as long without one).
PROBE_MIN_INTERVAL_S = 1.0
PROBE_BACKOFF_FACTOR = 2.0

# The closing-call note's guard tag + its leading text for the model.
GUARD = "goal-checkpoint"
SUMMARY_MARK = "[goal-checkpoint]"

# Read through a module attribute so tests can drive the debounce with a fake clock.
_now = time.monotonic


def after_tool_round(messages: list) -> bool:
    """True when the last thing in the turn is a tool result — a tool round just ran and
    the model is about to be called again."""
    return bool(messages) and isinstance(messages[-1], ToolMessage)


def _round_index(messages: list) -> int:
    """The number of model rounds so far — the same count for every hook call in one round."""
    return sum(1 for m in messages if isinstance(m, AIMessage))


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


async def _announce(text: str) -> None:
    """A transient status line on the live stream while the probe runs."""
    try:
        from langchain_core.callbacks import adispatch_custom_event

        await adispatch_custom_event("goal_probe", {"text": text})
    except Exception:  # noqa: BLE001 — a status line must never fail the turn
        log.debug("goal_probe status was not dispatched", exc_info=True)


def summary_note(reason: str) -> str:
    """The note the closing call runs on once the verifier passed mid-turn."""
    return (
        f"{SUMMARY_MARK} The goal's verifier just passed ({reason}). The goal is achieved — "
        "reply with one or two sentences summarising what you changed. Don't call tools; "
        "don't start anything new."
    )


async def goal_checkpoint(state) -> dict | None:
    """The ``before_model`` update for a goal-driven turn, or ``None`` (carry on).

    Right after a tool round, probe the goal's verifier (debounced). When it passes, mark
    the goal ACHIEVED now (``GoalController.finish_mid_turn`` — the post-turn drive then
    only reports it) and run ONE closing model call on a summary note, with tools unbound
    (``closing_call``) — so the reply ends on what changed, not on a sentence cut off
    before the next tool. Should that call still produce tool calls, they are dropped, and
    a tool round after the pass ends the turn outright. Never raises."""
    marker = current_goal_turn()
    messages = (state or {}).get("messages") or []
    if marker is None or not after_tool_round(messages):
        return None
    if marker.achieved_note:
        return {"jump_to": "end"}  # a tool round AFTER the pass: never more work
    round_ix = _round_index(messages)
    if round_ix == marker.probe_round or _now() < marker.probe_after:
        return None
    from runtime.state import STATE

    ctrl = STATE.goal_controller
    sid = _session_id(state)
    if ctrl is None or not sid or not hasattr(ctrl, "probe"):
        return None
    # Probe-eligible at all? (an llm verifier, or no active goal → nothing to do, no status)
    can_probe = getattr(ctrl, "can_probe", None)
    if can_probe is not None and not can_probe(sid):
        return None
    marker.probe_round = round_ix
    started = _now()
    await _announce("checking the goal…")
    try:
        result = await ctrl.probe(sid)
    except Exception:  # noqa: BLE001 — the probe is advisory
        log.warning("[goal] mid-turn probe raised for %s", sid, exc_info=True)
        result = None
    took = max(0.0, _now() - started)
    marker.probes += 1
    marker.probe_after = _now() + max(PROBE_MIN_INTERVAL_S, PROBE_BACKOFF_FACTOR * took)
    if result is None or not result.met:
        return None
    reason = result.reason or "verifier passed"
    try:
        note = await ctrl.finish_mid_turn(sid, result)
    except Exception:  # noqa: BLE001 — leave the outcome to the post-turn evaluate
        log.warning("[goal] could not record the mid-turn pass for %s", sid, exc_info=True)
        return {"jump_to": "end"}
    if not note:
        return {"jump_to": "end"}  # the goal went away meanwhile (cleared): just stop
    marker.achieved_note = note
    marker.closing = True
    marker.closing_reason = reason
    log.info("[goal] verifier passed mid-turn for %s (%s) — closing the turn", sid, reason)
    return {"messages": [guard_note(GUARD, summary_note(reason))]}


async def closing_call(request, handler):
    """``awrap_model_call`` body: the closing call after a mid-turn pass runs with NO tools
    bound, and any tool call it still produces is dropped — it can only reply. The turn
    stream drops that step's streamed tool calls too (``is_closing_call_event``), so no
    tool card opens that nothing would close.

    The goal is already recorded achieved, so a failing closing call (a 429, a timeout)
    must not fail the turn: it is logged with an error id and replaced by a one-line
    fallback reply — never retried — and the drive reports the pass as usual."""
    marker = current_goal_turn()
    if marker is None or not marker.closing:
        return await handler(request)
    marker.closing = False
    try:
        from langgraph.config import get_config

        marker.closing_step = (get_config().get("metadata") or {}).get("langgraph_step")
    except Exception:  # noqa: BLE001 — outside a graph run: nothing streams anyway
        marker.closing_step = None
    try:
        response = await handler(request.override(tools=[]))
    except Exception as exc:  # noqa: BLE001 — the goal is already met; never fail the turn now
        from langchain.agents.middleware.types import ModelResponse

        err_id = uuid.uuid4().hex[:8]
        log.warning(
            "[goal] closing call failed (error id %s): %s — ending on a fallback reply", err_id, exc, exc_info=True
        )
        return ModelResponse(result=[AIMessage(content=f"Goal met: {marker.closing_reason or 'verifier passed'}.")])
    for msg in getattr(response, "result", None) or ([response] if isinstance(response, AIMessage) else []):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            msg.tool_calls = []
            msg.invalid_tool_calls = []
    return response
