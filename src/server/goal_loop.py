"""The goal drive loop and the autonomous HITL auto-answer — ONE copy for both turn drivers.

Extracted from ``server/chat.py`` (#3884, epic #3804 slice 3). The streaming driver
(``_run_native_turn``) and the non-streaming driver (``_chat_langgraph_impl``'s
``_native_turn``, now in ``server/turn_sync.py``, #3917) each carried their own copy of this logic, and the copies drifted
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
  A pass the round governor ended at its round cap pauses the drive (#3957).
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

import contextlib
import importlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

from runtime.state import STATE
from server import turn_control as _turn_control

# The note the drive ends on when the agent handed the goal to a watch/schedule (ADR 0079).
PAUSE_NOTE = "⏸ goal paused — handed off to a watch/schedule; will resume when it fires."


def round_cap_note(marker) -> str:
    """The note the drive ends on when the round governor ended a goal-driven turn at its
    round cap (#3957). The goal stays ACTIVE — the next operator message (or a watch /
    schedule fire) drives it again — but the drive does not immediately re-run a turn
    that just ran away: each re-drive then costs at most one capped turn."""
    return (
        f"⏸ goal paused — round cap reached ({marker.rounds} model rounds in one turn; "
        f"{marker.cap_key}: {marker.round_cap}). The goal stays active: send a message to continue."
    )


def _thread_of(config: dict | None) -> str:
    return str(((config or {}).get("configurable") or {}).get("thread_id") or "")


async def record_goal_note(config: dict, note: str, *, pass_config: dict | None = None) -> bool:
    """Write the drive's terminal pause note onto the turn's checkpointed thread (#3957).

    The note reaches the caller only as a suffix on the turn's final TEXT (``GoalDrive.text``)
    — the stream's terminal frame, ``/v1``'s content. The checkpoint, which is what a
    transcript export (and a rebuilt chat, and the next turn's model) reads, held only the
    round governor's hand-back, so the export lost the one line that says the goal is
    paused and why.

    When the capped pass ran on this thread and ended on a plain text reply (the
    governor's hand-back), that message is rewritten in place — same id, so the
    ``add_messages`` reducer replaces it — to the text the stream showed: the hand-back,
    a rule, the note. Otherwise (a fresh-context goal's pass ran on its own scoped thread,
    or the tail is not a plain reply) the note is appended as its own assistant message.
    Tagged ``protoagent_goal_note`` either way. Best-effort: never raises."""
    graph = STATE.graph
    if graph is None or not (note or "").strip():
        return False
    try:
        from langchain_core.messages import AIMessage

        last = None
        if _thread_of(pass_config) == _thread_of(config):
            snap = await graph.aget_state(config)
            msgs = ((getattr(snap, "values", None) or {}) if snap is not None else {}).get("messages") or []
            last = msgs[-1] if msgs else None
        tagged = {"protoagent_goal_note": note}
        if (
            isinstance(last, AIMessage)
            and getattr(last, "id", None)
            and not getattr(last, "tool_calls", None)
            and isinstance(last.content, str)
        ):
            msg = AIMessage(
                content=f"{last.content}\n\n---\n{note}",
                id=last.id,
                additional_kwargs={**(last.additional_kwargs or {}), **tagged},
            )
        else:
            msg = AIMessage(content=note, additional_kwargs=tagged)
        await graph.aupdate_state(config, {"messages": [msg]})
        return True
    except Exception:  # noqa: BLE001 — bookkeeping must never break the drive
        import logging

        logging.getLogger(__name__).warning("[goal] could not record the pause note on the thread", exc_info=True)
        return False


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


def goal_fenced(goal_state, fence) -> list[str]:
    """The fence a GOAL-DRIVEN pass runs under: the turn's ``fence`` intersected with the
    fence of the turn that SET the goal (``GoalState.fence``, narrowest wins). A goal a
    fenced turn set is pursued fenced by every later turn that drives it — even a plain
    operator turn — on both drivers. No goal / an unfenced goal → ``fence`` unchanged."""
    from graph.fence_scope import normalize_fence
    from graph.middleware.subagent_fence import intersect_fences

    if goal_state is None:
        return list(fence or [])
    return intersect_fences(list(fence or []), normalize_fence(getattr(goal_state, "fence", None)))


def goal_model_notice(model: str) -> str:
    return (
        f"⚠ The goal's model `{model}` is unavailable — running on the default model. "
        "Pick a model, or `/goal clear`."
    )


# Inherited picks that test-built recently: model → monotonic time of the last good build.
# The probe is only needed to catch a pick that broke; a few minutes' staleness is covered
# by the middleware's call-time fallback (a pick refused mid-turn still drops to default).
_PROBE_OK: dict[str, float] = {}
_PROBE_TTL_S = 300.0


async def resolve_turn_model(session_id: str, request_model: str | None) -> tuple[str, str]:
    """``(effective model pick, notice)`` for a turn (#3957). Called once, at turn start, by
    both drivers; the result is what the lead graph is stamped with AND what
    ``turn_model_scope`` binds — so ``/<workflow>`` steps, ``sdk.run_subagent`` and every
    delegation follow the same model the lead runs on.

    - A pick carried by THIS request wins. It may hard-fail the turn (the middleware's
      400/503), and on a turn that drives an active goal it becomes the goal's model, so
      the next re-drive without a pick uses it.
    - No pick, and an active goal set with one (``GoalState.model``): the turn inherits
      it — a watch / schedule fire, a self-resume, a "Default" message. An INHERITED pick
      must never lock the goal into failing: it is test-built here first, and if it can't
      be built the turn runs on the default with ``notice`` saying so (logged at WARNING).
    - Neither: ``""`` — the configured default."""
    own = (request_model or "").strip()
    goal = active_goal(session_id)
    if own:
        if goal is not None and getattr(goal, "model", "") != own:
            remember = getattr(STATE.goal_controller, "remember_model", None)
            if remember is not None:
                try:
                    remember(session_id, own)
                except Exception:  # noqa: BLE001 — bookkeeping never fails the turn
                    pass
        return own, ""
    inherited = str(getattr(goal, "model", "") or "").strip() if goal is not None else ""
    if not inherited:
        return "", ""
    import asyncio
    import time

    ok_at = _PROBE_OK.get(inherited)
    if ok_at is not None and time.monotonic() - ok_at < _PROBE_TTL_S:
        return inherited, ""
    try:
        from graph import llm as _llm

        # Off the event loop: building a native-OAuth client may refresh its token over the
        # network (a synchronous request with a 20s timeout), which must not stall every
        # other turn on this worker.
        await asyncio.to_thread(_llm.create_llm, STATE.graph_config, model_name=inherited)
    except Exception as exc:  # noqa: BLE001 — any failure: fall back, never lock the goal in
        _PROBE_OK.pop(inherited, None)
        import logging

        logging.getLogger(__name__).warning(
            "[goal] the goal's model %r is unavailable for session %s (%s) — running this turn on the default",
            inherited,
            session_id,
            exc,
        )
        return "", goal_model_notice(inherited)
    _PROBE_OK[inherited] = time.monotonic()
    return inherited, ""


def kickoff_message(goal_state, message: str, *, resume: bool, overflow_retry: bool = False) -> str:
    """Kickoff injection (#1910): the FIRST goal-driven turn (iteration 0, not a HITL
    resume) carries the goal condition — the raw user text folded into the kickoff prompt —
    so the agent begins on the goal instead of asking "what goal?". Later iterations get
    the goal via the continuation prompt, and a resume's message is an answer, not a turn.

    The context-overflow retry (``overflow_retry``) is never wrapped either (#3891 F1): the
    kickoff applies to the OPERATOR's message, and the retry re-runs that same turn with
    the recovery prompt — folding the recovery prompt in as the "user message" of a second
    kickoff would hand the agent a goal statement that isn't the operator's."""
    if goal_state is None or resume or overflow_retry or goal_state.iteration != 0:
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
    output before asking for the next step; an empty ``text`` keeps the previous answer. It also sets
    ``goal_pass`` to the continuation's ``goal_turn()`` marker, so a pass the round governor
    capped pauses the drive (#3957)."""

    message: str
    config: dict
    text: str = ""
    goal_pass: Any = None


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
        # The ``goal_turn()`` marker of the pass just run (graph.goals.goal_turn.GoalTurn).
        # The driver sets it after the initial pass; each continuation's rides the step.
        self.last_pass = None
        # The config the last pass ran on — the turn's own, or a fresh-context goal's
        # scoped ``…:goal-iter-N`` thread (``record_goal_note`` needs to know which).
        self.last_pass_config: dict | None = config
        # An async-context-manager factory held around the pause-note write (#3957) — the
        # thread lock, for a driver that does NOT already hold it across the drive (the
        # non-streaming one locks per pass). ``None``: the caller already holds it.
        self.note_lock = None
        # Set by the driver when the turn's inherited goal model fell back (#3957).
        self.notice = ""

    async def steps(self) -> AsyncIterator[GoalNote | GoalContinuation]:
        # The inherited-model fallback notice (#3957, ``resolve_turn_model``): a status
        # line up front for the streaming surface, and a line on the final text for all.
        if self.notice:
            yield GoalNote(self.notice)
        async for step in self._steps():
            yield step
        notice = self.notice
        if not notice:
            # ...or the provider rejected the inherited pick mid-turn (the middleware fell
            # back to the default and flagged the turn's marker).
            from graph.subagent_model import current_inherited_pick

            pick = current_inherited_pick()
            if pick is not None and pick.fell_back:
                notice = goal_model_notice(pick.model)
        if notice:
            self.text = f"{self.text}\n\n{notice}"

    def _met_mid_turn(self) -> str:
        """The terminal note when the pass just run already met — and recorded — the goal
        mid-turn (``graph.middleware.goal_checkpoint``); ``""`` otherwise."""
        return str(getattr(self.last_pass, "achieved_note", "") or "") if self.last_pass is not None else ""

    async def _steps(self) -> AsyncIterator[GoalNote | GoalContinuation]:
        if met := self._met_mid_turn():
            yield GoalNote(met)
            self.text = f"{self.text}\n\n---\n{met}"
            return
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
            if self.last_pass is not None and self.last_pass.capped:
                # The pass just run hit the per-turn round cap (#3957): don't re-drive a
                # turn that ran away — pause (goal stays active) and say why. The verifier
                # already ran above, so a capped turn that MET the goal still finishes.
                note = round_cap_note(self.last_pass)
                _record = getattr(STATE.goal_controller, "note_round_cap", None)
                if _record is not None:
                    _record(self.session_id, note)
                async with self.note_lock() if self.note_lock is not None else contextlib.nullcontext():
                    await record_goal_note(self.config, note, pass_config=self.last_pass_config)
                yield GoalNote(note)
                break
            step = GoalContinuation(decision.message, _chat()._goal_continuation_config(self.config, decision.state))
            yield step
            self.last_pass = step.goal_pass
            self.last_pass_config = step.config
            if step.text:
                self.text = step.text
            if met := self._met_mid_turn():
                # The continuation met the goal mid-turn: it is already recorded achieved.
                note = met
                yield GoalNote(met)
                break
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
