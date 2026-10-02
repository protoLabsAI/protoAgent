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

from langchain_core.messages import AIMessage, ToolMessage

from graph.goals.goal_turn import current_goal_turn

log = logging.getLogger(__name__)

# The shortest gap between two mid-turn probes of one pass, in seconds, and the back-off
# for a slow verifier (the gap is at least this many times the last probe's duration).
PROBE_MIN_INTERVAL_S = 10.0
PROBE_BACKOFF_FACTOR = 2.0

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


async def goal_met_after_tools(state) -> bool:
    """On a goal-driven turn, right after a tool round, probe the goal's verifier (debounced);
    True when it passes. Never raises."""
    marker = current_goal_turn()
    messages = (state or {}).get("messages") or []
    if marker is None or not after_tool_round(messages):
        return False
    round_ix = _round_index(messages)
    if round_ix == marker.probe_round or _now() < marker.probe_after:
        return False
    from runtime.state import STATE

    ctrl = STATE.goal_controller
    sid = _session_id(state)
    if ctrl is None or not sid or not hasattr(ctrl, "probe"):
        return False
    # Probe-eligible at all? (an llm verifier, or no active goal → nothing to do, no status)
    can_probe = getattr(ctrl, "can_probe", None)
    if can_probe is not None and not can_probe(sid):
        return False
    marker.probe_round = round_ix
    started = _now()
    await _announce("checking the goal…")
    result = await ctrl.probe(sid)
    took = max(0.0, _now() - started)
    marker.probes += 1
    marker.probe_after = _now() + max(PROBE_MIN_INTERVAL_S, PROBE_BACKOFF_FACTOR * took)
    if result is None or not result.met:
        return False
    log.info("[goal] verifier passed mid-turn for %s (%s) — ending the turn", sid, result.reason)
    return True
