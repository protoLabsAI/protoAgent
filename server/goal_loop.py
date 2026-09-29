"""The goal drive loop and the autonomous HITL auto-answer — ONE copy for both turn drivers.

Extracted from ``server/chat.py`` (#3884, epic #3804 slice 3). The streaming driver
(``_run_native_turn``) and the non-streaming driver (``_chat_langgraph_impl``'s
``_native_turn``) each carried their own copy of this logic, and the copies drifted
(#3872: the non-streaming auto-answer resumed with a bare value, not keyed by interrupt id).

The two drivers differ in SHAPE — the streaming one yields frames while a graph pass runs,
the non-streaming one awaits ``ainvoke`` and returns a reply — so the shared pieces own the
DECISIONS and leave the running of a graph pass to the driver:

* :func:`active_goal` / :func:`kickoff_message` — the goal state a turn starts under, and
  the #1910 kickoff injection (iteration 0, never on a HITL resume).
* :class:`GoalDrive` — the verify-and-continue loop (#1910/ADR 0090): evaluate, stop on
  done / no decision / async handoff (ADR 0079) / the hard cap, build each continuation's
  config (fresh-context goals get a scoped thread), and append the terminal note.
  :meth:`GoalDrive.steps` is an async generator of :class:`GoalNote` (a status line; the
  streaming driver shows it, the other has no status surface) and
  :class:`GoalContinuation` (run this continuation, then set ``step.text`` to its output).
* :class:`HitlAutoAnswer` — the autonomous no-deadlock policy (#1911): park / answer with
  the no-operator sentinel / give up and clear, within the ``_MAX_AUTONOMOUS_AUTOANSWERS``
  budget. Every resume is keyed by interrupt id through ``server.chat._resume_payload``
  (the #3872 fix). :meth:`HitlAutoAnswer.settle` is the whole loop for an ``ainvoke``-style
  driver; the streaming driver calls :meth:`~HitlAutoAnswer.on_interrupt` per
  ``input_required`` frame and resumes through ``_run_turn_stream(resume_value=…)``, which
  keys the resume the same way.

**Collaborators stay where they live and are read at CALL time**, so a test's patch on the
owner is what runs (``tests/test_goal_loop_seam.py``): ``server.chat``'s
``_awaiting_self_resume`` / ``_goal_continuation_config`` / ``_pending_interrupt_value`` /
``_resume_payload`` / ``_clear_pending_interrupt`` via :func:`_chat`, and
``server.turn_control``'s autonomy classifier, cap and sentinel via ``_turn_control.<name>``.
There is no import-time edge back into ``server.chat``.
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from runtime.state import STATE
from server import turn_control as _turn_control

# The note the drive ends on when the agent handed the goal to a watch/schedule (ADR 0079).
PAUSE_NOTE = "⏸ goal paused — handed off to a watch/schedule; will resume when it fires."

# HitlAutoAnswer.on_interrupt verdicts.
PARK = "park"
ANSWER = "answer"
GIVE_UP = "give_up"


def _chat() -> ModuleType:
    """``server.chat`` the MODULE, resolved at call time — by path, because ``server``
    re-exports the ``chat`` FUNCTION under the submodule's name."""
    return importlib.import_module("server.chat")


# ── goal state at turn start ─────────────────────────────────────────────────────────


def active_goal(session_id: str):
    """The session's active goal state, or ``None`` (no controller, or no active goal).
    A goal active when the turn starts makes the whole turn goal-driven."""
    ctrl = STATE.goal_controller
    return ctrl.active_goal(session_id) if ctrl is not None else None


def kickoff_message(goal_state, message: str, *, resume: bool) -> str:
    """Kickoff injection (#1910): the FIRST goal-driven turn (iteration 0, not a HITL
    resume) carries the goal condition — the raw user text folded into the kickoff prompt —
    so the agent begins on the goal instead of asking "what goal?". Later iterations get
    the goal via the continuation prompt, and a resume's message is an answer, not a turn."""
    if goal_state is None or resume or goal_state.iteration != 0:
        return message
    return STATE.goal_controller.kickoff_prompt(goal_state, user_message=message)


# ── the goal drive loop ──────────────────────────────────────────────────────────────


@dataclass
class GoalNote:
    """A goal status line (the verifier's note, or the pause note). Surface-specific: the
    streaming driver emits it as a ``🎯`` status frame; the non-streaming one has no status
    surface and only the terminal note (appended to the reply) reaches its caller."""

    note: str


@dataclass
class GoalContinuation:
    """Run one continuation turn: ``message`` on ``config`` (fresh-context goals get a
    scoped per-iteration thread). The driver sets ``text`` to the continuation's extracted
    output before asking for the next step; an empty ``text`` keeps the previous answer."""

    message: str
    config: dict
    text: str = ""


class GoalDrive:
    """Verify the outcome after the agent stops; while not met, re-invoke with a
    continuation prompt until the verifier passes, the iteration budget is spent, it's
    flagged unachievable, or the agent handed off to a watch/schedule (then PAUSE — the
    goal stays active and the trigger's fire resumes the drive).

    ``text`` is the running answer: seeded with the turn's reply, replaced by each
    non-empty continuation output, and — once :meth:`steps` is exhausted — suffixed with
    the terminal goal note (so the reply / A2A terminal artifact carries the outcome)."""

    def __init__(self, session_id: str, config: dict, text: str):
        self.session_id = session_id
        self.config = config
        self.text = text

    async def steps(self) -> AsyncIterator[GoalNote | GoalContinuation]:
        if STATE.goal_controller is None or not STATE.goal_controller.active_goal(self.session_id):
            return
        # Hard cap on top of the controller's own budget: a verifier that never says
        # "done" (or a controller that never exhausts) can't spin the turn forever.
        guard, hard_cap = 0, STATE.graph_config.goal_max_iterations + 2
        note = ""
        while guard < hard_cap:
            guard += 1
            decision = await STATE.goal_controller.evaluate(self.session_id, last_text=self.text)
            if decision is None:
                break
            note = decision.note
            yield GoalNote(decision.note)
            if decision.action == "done":
                break
            if _chat()._awaiting_self_resume(self.session_id):
                # Async handoff (ADR 0079): pause the drive rather than spinning to the cap.
                note = PAUSE_NOTE
                yield GoalNote(note)
                break
            step = GoalContinuation(decision.message, _chat()._goal_continuation_config(self.config, decision.state))
            yield step
            if step.text:
                self.text = step.text
        if note:
            self.text = f"{self.text}\n\n---\n{note}"


# ── the autonomous HITL auto-answer ──────────────────────────────────────────────────


def is_autonomous_turn(request_metadata: dict | None, *, goal_active: bool) -> bool:
    """No operator watching → the turn must never deadlock on a HITL pause. A goal-driven
    turn is autonomous BY DEFINITION (#1911): a goal is an explicit opt-in to self-drive,
    so the first "what goal?" ask must not park it (the #1910 deadlock)."""
    return _turn_control._is_autonomous(request_metadata) or goal_active


class HitlAutoAnswer:
    """The autonomous no-deadlock policy for one turn's HITL interrupts.

    An attended turn PARKS (a human answers). An autonomous one ANSWERS with the
    no-operator sentinel and re-runs, up to ``_MAX_AUTONOMOUS_AUTOANSWERS`` times; still
    asking after that, it GIVES UP: the stray interrupt is cleared so the checkpoint isn't
    left dangling, and the turn completes on whatever text it has — it must never park."""

    def __init__(self, autonomous: bool):
        self.autonomous = autonomous
        self.answered = 0

    def on_interrupt(self) -> str:
        """The verdict for an interrupt the turn just hit: PARK, ANSWER or GIVE_UP."""
        if not self.autonomous:
            return PARK
        if self.answered < _turn_control._MAX_AUTONOMOUS_AUTOANSWERS:
            return ANSWER
        return GIVE_UP

    def answer(self):
        """Spend one auto-answer; the RAW sentinel to resume with. The resume is keyed by
        the pending interrupt's id where it's built (``server.chat._resume_payload`` — a
        bare resume with several pending interrupts is a hard LangGraph error, #3872)."""
        self.answered += 1
        return _turn_control._AUTONOMOUS_HITL_SENTINEL

    async def give_up(self, config: dict) -> None:
        await _chat()._clear_pending_interrupt(config)

    async def settle(self, config: dict, result: Any, invoke: Callable[[Any], Awaitable[Any]]) -> Any:
        """For an ``ainvoke``-style driver: while the thread is parked at an interrupt,
        apply the policy — resume past it (``invoke(Command(resume={id: sentinel}))``,
        whose result replaces ``result``) or give up and clear. An attended turn is left
        untouched (the interrupt stays pending for the driver to surface). Returns the
        latest result."""
        from langgraph.types import Command

        if not self.autonomous:
            return result  # attended: nothing to settle — the driver surfaces the ask
        chat = _chat()
        while await chat._pending_interrupt_value(config) is not None:
            if self.on_interrupt() == GIVE_UP:
                await self.give_up(config)
                break
            result = await invoke(Command(resume=await chat._resume_payload(config, self.answer())))
        return result
