"""Restart-interrupted turn recovery — ADR 0129 D7 (S6).

When the server stops mid-turn (SIGTERM, crash, desktop relaunch), boot
reconciliation (``a2a_impl.stores.reconcile_interrupted_tasks``) used to mark EVERY
non-terminal task ``failed`` — at-most-once, right for side effects, but it silently
lost scheduler-owned work; the worst case is a background ``wait``/watch continuation,
the agent's only way back into a loop. D7 replaces blanket failure for scheduler-owned
work (job sessions, wait:/watch continuations): on boot those tasks are SPARED, each
is classified from its LangGraph checkpoint — the source of truth for what completed —
and the safe cases resume automatically against the original thread and task handle,
never a new prompt. Interactive chat turns keep today's behavior.

Classification, from the checkpoint's messages:

- **clean** — between steps or during a model call: every tool call in the last AI
  message has its ToolMessage. Resuming repeats nothing that completed.
- **mid_tool_call** — an AI message has tool calls without results. The call's effect
  is unknown; resuming reports it as interrupted rather than re-running it.
- **stale_checkpoint** — the thread never received THIS fire's prompt: the kill landed
  after the fire's task handle was saved (``returnImmediately``) but before the graph
  checkpointed the new turn, so a reused thread (a recurring ``job:`` session, a
  ``wait:``/watch continuation) still holds only the PREVIOUS turn's messages. However
  finished that tail looks, nothing of this fire is recoverable — resuming it would
  complete the task with another turn's reply.
- **no_checkpoint** — nothing readable to resume from.

The POLICY (what resumes, what waits for an operator, the one-resume cap counted on
the occurrence row) lives in the scheduler's boot recovery
(``scheduler/local.py::LocalScheduler._recover_interrupted_turns``). The scheduler
must not import ``server``, so it reaches this module only through the duck-typed
recovery object ``note_interrupted_tasks`` injects (wired in ``server/__init__``).
This module owns what needs server-side state: the checkpointer (``STATE.graph``),
the per-thread turn lock, and the durable task store (``STATE.a2a_task_engine``).

S7 adds the operator's side of ``interrupted_needs_operator``: the scheduler's disposal
raises one Needs-you decision in the job's session (:meth:`InterruptedTurnRecovery.
raise_needs_operator_decision`), and answering it performs Resume / Re-run / Drop exactly
once (:meth:`InterruptedTurnRecovery.operator_action`) — reached by the decision routes
through ``STATE.interrupted_recovery``, since ``operator_api`` must not import ``server``.
Each resume that recovers a turn also appends a ``Resumed <job> after a restart (step N)``
Activity entry.
"""

from __future__ import annotations

import json
import logging

from runtime.state import STATE

log = logging.getLogger(__name__)

# Classification results (the scheduler's recovery pass switches on these strings).
CLEAN = "clean"
MID_TOOL_CALL = "mid_tool_call"
NO_CHECKPOINT = "no_checkpoint"
STALE_CHECKPOINT = "stale_checkpoint"

#: The uncertainty report appended as the ToolMessage for each tool call a restart
#: left dangling (D7): the tool may or may not have run; only the model can decide
#: how to verify. Deliberately an instruction, not an error dump.
INTERRUPTED_TOOL_RESULT = (
    "This tool call was interrupted by a restart; its effect is unknown — verify before repeating it."
)

#: The tools node's name in the compiled agent graph (``create_agent``'s ToolNode).
#: ``aupdate_state(as_node=...)`` records the dangling-call reports as if that node
#: had produced them, so the resume continues to the model, not back into the tools.
_TOOLS_NODE = "tools"


def _dangling_tool_calls(messages) -> list[dict]:
    """Tool calls in the LAST AI message that have no ToolMessage result — the D7
    mid-tool-call signature. Earlier AI messages are complete in any checkpoint the
    graph committed (a model step only runs after the previous tools step wrote its
    results), so only the last one can dangle."""
    from langchain_core.messages import AIMessage, ToolMessage

    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    if last_ai is None:
        return []
    return [c for c in (getattr(last_ai, "tool_calls", None) or []) if c.get("id") and c["id"] not in answered]


def _fire_anchor_index(messages, *, fired_after, prompt: str) -> int | None:
    """The index of THIS fire's wake prompt in the thread, or ``None`` if it never landed.

    The fire's task handle is saved (``returnImmediately``) before the graph
    checkpoints the wake prompt, so a kill in that window leaves a reused thread
    holding only the previous turn's finished messages — a tail that reads ``clean``
    and whose resume would finalize the task with the PRIOR turn's reply. Every
    HumanMessage the turn drivers write is stamped ``protoagent_sent_at`` at write
    time (``server/chat.py::_vision_human_message``), so the fire reached the
    checkpoint iff some HumanMessage stamped at/after the fire's due time carries the
    fire's prompt text. Both signals matter: a recurring job's prompt is IDENTICAL
    across slots (only the timestamp tells slot N from slot N-1), and a timestamp
    alone can't tell this fire's prompt from an unrelated message that landed on the
    thread after boot. The stamp truncates to whole seconds, so the anchor is floored
    before comparing. An unstamped or unparseable message proves nothing and is
    skipped (fail closed). No anchor and no prompt to check against → index 0: a
    degraded caller keeps the pre-check behavior and reads the whole thread as the
    turn."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage

    from graph.compaction_engine import SENT_AT_KWARG, message_text

    want = (prompt or "").strip()
    if fired_after is None and not want:
        return 0
    floor = fired_after.replace(microsecond=0) if fired_after is not None else None
    for i, m in enumerate(messages):
        if not isinstance(m, HumanMessage):
            continue
        if want and want not in message_text(m):
            continue
        if floor is not None:
            raw = (getattr(m, "additional_kwargs", None) or {}).get(SENT_AT_KWARG)
            try:
                sent = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if sent.tzinfo is None:
                sent = sent.replace(tzinfo=UTC)
            if sent < floor:
                continue
        return i
    return None


def _fire_reached_checkpoint(messages, *, fired_after, prompt: str) -> bool:
    """Did THIS fire's prompt land in the checkpoint? (:func:`_fire_anchor_index`.)"""
    return _fire_anchor_index(messages, fired_after=fired_after, prompt=prompt) is not None


#: Option ids of the S7 Needs-you recovery decision (ADR 0129 D7). ``resume`` is offered
#: only when the checkpoint holds this fire's own turn; with no (or a stale) checkpoint the
#: operator gets Re-run and Drop only — there is nothing to continue from.
RESUME_OPTION = "resume"
RERUN_OPTION = "rerun"
DROP_OPTION = "drop"


def _job_title(prompt: str, job_id: str) -> str:
    """A short human label for the job: the prompt's first line, clipped; the id as fallback."""
    line = next((ln.strip() for ln in (prompt or "").splitlines() if ln.strip()), "")
    if not line:
        return job_id or "A scheduled job"
    return line if len(line) <= 80 else line[:77] + "…"


def _turn_progress(messages, anchor: int) -> tuple[int, str]:
    """``(completed model steps, last tool action)`` for the turn starting at ``anchor``.

    A step is one model pass (one AI message); the last action is the most recent AI
    message's final tool call rendered ``name({short args})`` — empty when the turn never
    called a tool."""
    from langchain_core.messages import AIMessage

    turn = messages[anchor:]
    steps = sum(1 for m in turn if isinstance(m, AIMessage))
    for m in reversed(turn):
        if not isinstance(m, AIMessage):
            continue
        calls = getattr(m, "tool_calls", None) or []
        if calls:
            args = json.dumps(calls[-1].get("args") or {}, ensure_ascii=False, sort_keys=True)
            if len(args) > 80:
                args = args[:77] + "…"
            return steps, f"{calls[-1].get('name') or 'tool'}({args})"
    return steps, ""


def _final_text(messages) -> str:
    """The last AI message's text — what the finished turn would have reported."""
    from langchain_core.messages import AIMessage

    for m in reversed(messages):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "".join(
                    part.get("text", "") if isinstance(part, dict) else str(part) for part in content
                )
    return ""


async def _finalize_task(task_id: str, *, state: str, text: str) -> None:
    """Move the spared task row to ``state`` (CAS on the interrupted states)."""
    engine = getattr(STATE, "a2a_task_engine", None)
    if engine is None:
        raise RuntimeError("a2a task engine unavailable; cannot finalize task")
    from a2a_impl.stores import finalize_interrupted_task

    await finalize_interrupted_task(engine, task_id, state=state, text=text)


class InterruptedTurnRecovery:
    """The checkpoint/task-store side of the scheduler's D7 boot recovery.

    Injected into ``LocalScheduler.note_interrupted_tasks`` as a duck-typed object
    (the scheduler never imports server). Every method fails safe: an unreadable
    checkpoint classifies as ``no_checkpoint`` (→ operator disposition), a resume
    that cannot act returns False so the scheduler quarantines the occurrence
    instead of guessing.
    """

    def _thread_config(self, context_id: str) -> dict:
        from graph.thread_ids import resolve_thread_id

        return {
            "configurable": {"thread_id": resolve_thread_id(None, context_id)},
            "recursion_limit": getattr(STATE.graph_config, "max_iterations", 200) or 200,
        }

    def _resume_metadata(self, task_id: str, *, job_id: str, origin: str, fence) -> dict:
        """The per-turn request metadata the interrupted turn originally ran under: the
        scheduler's fire metadata (``scheduler/local.py::_fire``) merged with the
        executor's ``a2a.task_id`` stamp (``a2a_impl/executor.py``). The fence key is
        wire parity only — enforcement rides the graph state the original pass already
        stamped (``graph/middleware/subagent_fence.py``), which the resume continues."""
        md = {
            "scheduler_kind": "local",
            "origin": origin or "scheduler",
            "a2a.task_id": task_id,
        }
        if job_id:
            md["scheduler_job_id"] = job_id
        if fence:
            md["subagent_fence"] = list(fence)
        return md

    async def classify(self, context_id: str, *, fired_after=None, prompt: str = "") -> str:
        """``clean`` / ``mid_tool_call`` / ``stale_checkpoint`` / ``no_checkpoint`` for
        the session's thread. ``fired_after`` (the occurrence's due time, a datetime)
        and ``prompt`` (the job's prompt text) identify THIS fire's wake prompt in the
        thread (:func:`_fire_reached_checkpoint`): a checkpoint that never received it
        is stale — the kill landed between the task-handle save and the graph's first
        checkpoint of the new turn — and is checked BEFORE the dangling-call shape,
        which would otherwise be read off another turn's tail."""
        graph = STATE.graph
        if graph is None or not context_id:
            return NO_CHECKPOINT
        try:
            snap = await graph.aget_state(self._thread_config(context_id))
        except Exception:  # noqa: BLE001 — unreadable is a classification, not a crash
            log.exception("[interrupted-turns] checkpoint unreadable for %s", context_id)
            return NO_CHECKPOINT
        messages = (getattr(snap, "values", None) or {}).get("messages") or []
        if not messages:
            return NO_CHECKPOINT
        if not _fire_reached_checkpoint(messages, fired_after=fired_after, prompt=prompt):
            return STALE_CHECKPOINT
        return MID_TOOL_CALL if _dangling_tool_calls(messages) else CLEAN

    async def resume(
        self,
        context_id: str,
        task_id: str,
        *,
        report_dangling: bool = False,
        fired_after=None,
        prompt: str = "",
        job_id: str = "",
        origin: str = "scheduler",
        fence=None,
    ) -> bool:
        """Drive the thread from its checkpoint to its end — never a new prompt.

        ``report_dangling`` (the wait:/watch mid-tool-call case) first records one
        ToolMessage per dangling call — :data:`INTERRUPTED_TOOL_RESULT` — as the tools
        node's output, so the model learns the call was interrupted instead of the
        graph re-running it. The original task handle is then moved to the state the
        turn actually ended in (completed / failed / an ``input_required`` pause), so
        the scheduler's normal reconciliation settles the occurrence exactly once.

        ``fired_after`` + ``prompt`` re-prove UNDER THE LOCK that the checkpoint holds
        this fire's own prompt (:func:`_fire_reached_checkpoint`, same check as
        :meth:`classify`): a stale thread — the kill predated the graph's first
        checkpoint of the turn — returns False, because both the "drive to the end"
        and the "turn had actually finished" paths below would otherwise finish
        ANOTHER turn and record its reply as this task's outcome.

        ``job_id`` / ``origin`` / ``fence`` rebuild the per-turn request metadata the
        live fire's turn ran under (the scheduler's A2A metadata plus the executor's
        ``a2a.task_id``), bound via ``request_metadata_scope`` for the drive: without
        it ``graph/projection.py::_is_firing_now`` can't recognise the job being
        resumed — its still-listed row reads as ANOTHER pending schedule ("runs
        automatically unless you cancel it", the documented watch-loop bait),
        ``chat._is_spent_firing_job`` misreads it the same way, and PromptCapture
        loses the ``a2a.task_id`` link for its snapshots.

        A D7 resume is a scheduler-owned turn (a scheduler / watch / inbox fire), so it
        follows the same HITL rule the live turn drivers apply to those origins
        (``turn_control._AUTONOMOUS_ORIGINS``): an ``ask_human`` / ``request_user_input``
        pause is auto-answered with the no-operator sentinel and the turn re-run, up to
        the shared cap, then given up — never parked (an un-parked occurrence is what
        lets the owner admit its next fire, D6). The ONE pause that does park is a
        ``request_decision`` (#4725), and it parks WITH its durable decisions-ledger
        record, exactly as the live drivers park it — visible in the operator inbox,
        resumable, expiring; never an anonymous input-required.

        Returns True when the thread was driven (whatever the outcome — the task
        handle now reflects it); False when nothing could be done, so the scheduler
        quarantines the occurrence for the operator instead.
        """
        graph = STATE.graph
        if graph is None or not context_id or not task_id:
            return False
        import importlib

        from graph.middleware.request_context import request_metadata_scope

        from server import goal_loop, turn_control

        # By path, not ``from server import chat``: the ``server`` package re-exports
        # the ``chat`` FUNCTION under the submodule's name (see goal_loop._chat()).
        chat_mod = importlib.import_module("server.chat")

        config = self._thread_config(context_id)
        tid = config["configurable"]["thread_id"]
        try:
            # The same per-thread lock every turn driver holds (server/chat.py): the
            # resume is a turn on this thread, serialized against any other writer.
            async with turn_control._thread_lock(tid):
                snap = await graph.aget_state(config)
                messages = (getattr(snap, "values", None) or {}).get("messages") or []
                if not messages:
                    return False
                anchor = _fire_anchor_index(messages, fired_after=fired_after, prompt=prompt)
                if anchor is None:
                    # The checkpoint predates this fire (classification raced, or was
                    # skipped): nothing of this fire is recoverable — leave it for the
                    # operator rather than finish another turn under this handle.
                    return False
                # Where the turn stood when the restart cut it down — the Activity
                # entry's "(step N)" (D7 S7).
                steps = _turn_progress(messages, anchor)[0]
                dangling = _dangling_tool_calls(messages)
                if dangling:
                    if not report_dangling:
                        # Policy said resume-only-clean; the thread disagrees (it moved,
                        # or classification raced) — leave it for the operator.
                        return False
                    await self._seal_dangling_calls(graph, config, dangling)
                if dangling or getattr(snap, "next", None):
                    # The drive runs under the SAME per-turn request metadata the live
                    # fire's turn carried (see the docstring) — the resumed turn's
                    # projection, goal pause and prompt snapshots behave as if the fire
                    # had never been cut down.
                    with request_metadata_scope(self._resume_metadata(task_id, job_id=job_id, origin=origin, fence=fence)):
                        result = await graph.ainvoke(None, config)
                        # The shared autonomous policy (one copy with the live drivers):
                        # answer each HITL pause with the sentinel and re-run; a decision
                        # stays pending; past the cap, clear the pause and complete.
                        auto = goal_loop.HitlAutoAnswer(True)
                        result = await auto.settle(config, result, lambda value: graph.ainvoke(value, config))
                        pending = await chat_mod._pending_interrupt_value(config)
                    if pending is not None and turn_control.is_decision_interrupt(pending):
                        # The resumed turn stopped at a choice it may not make on its
                        # own (#4725): record the durable ledger entry (inbox card,
                        # expiry, answer-resumes), then hold the handle at
                        # input_required — the claim stays unresolved, so the owner
                        # admits nothing else (D6), and the pause survives TTL sweeps.
                        await turn_control.decision_pause_payload(context_id, pending, config)
                        await _finalize_task(
                            task_id,
                            state="input_required",
                            text=str(pending.get("question") or "")
                            or "Resumed after a restart and paused for a decision.",
                        )
                        log.warning(
                            "[interrupted-turns] resumed %s after a restart; it parked on a decision (task %s)",
                            context_id,
                            task_id,
                        )
                        self._note_resumed(
                            context_id=context_id, task_id=task_id, job_id=job_id,
                            origin=origin, prompt=prompt, steps=steps,
                        )
                        return True
                    final = _final_text((result or {}).get("messages") or []) if isinstance(result, dict) else ""
                else:
                    # The checkpoint says the turn had actually finished — the restart
                    # only cut down the runner before the handle was finalized.
                    final = _final_text(messages)
        except Exception as exc:  # noqa: BLE001 — a failed resume is a failed turn, not a resend
            log.exception("[interrupted-turns] resume failed for %s (task %s)", context_id, task_id)
            try:
                await _finalize_task(task_id, state="failed", text=f"Resume after a restart failed: {exc}")
            except Exception:  # noqa: BLE001
                log.exception("[interrupted-turns] could not record the failed resume for task %s", task_id)
                return False
            return True
        await _finalize_task(
            task_id, state="completed", text=final or "(resumed after a restart; the turn ended without reply text)"
        )
        log.warning("[interrupted-turns] resumed %s after a restart (task %s)", context_id, task_id)
        self._note_resumed(
            context_id=context_id, task_id=task_id, job_id=job_id, origin=origin, prompt=prompt, steps=steps,
        )
        return True

    def _note_resumed(self, *, context_id: str, task_id: str, job_id: str, origin: str, prompt: str, steps: int) -> None:
        """Append the ``Resumed <job> after a restart (step N)`` Activity entry (D7 S7):
        one per resume that actually recovered the turn — the operator's provenance that
        a restart didn't silently swallow the job. Best-effort: the feed is never a gate."""
        feed = getattr(STATE, "activity_log", None)
        if feed is None:
            return
        try:
            feed.add(
                context_id=context_id,
                origin=origin or "scheduler",
                trigger=job_id or "",
                text=f"Resumed {_job_title(prompt, job_id)} after a restart (step {steps})",
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001 — provenance, never a gate
            log.exception("[interrupted-turns] could not record the resume Activity entry for %s", job_id)

    async def _seal_dangling_calls(self, graph, config: dict, dangling: list[dict]) -> None:
        """Record the interruption report as the tools node's output for each dangling
        call, so the thread is well-formed (every tool call answered) before the model
        runs again."""
        from langchain_core.messages import ToolMessage

        reports = [
            ToolMessage(
                content=INTERRUPTED_TOOL_RESULT,
                tool_call_id=call["id"],
                name=call.get("name") or "tool",
                status="error",
            )
            for call in dangling
        ]
        await graph.aupdate_state(config, {"messages": reports}, as_node=_TOOLS_NODE)

    async def seal_dangling(self, context_id: str) -> bool:
        """Append the interruption reports WITHOUT resuming (the recurring-skip case):
        the next scheduled occurrence reuses this execution session's thread, and a
        dangling tool call would hand the provider a malformed conversation. Returns
        True when the thread is well-formed afterwards."""
        graph = STATE.graph
        if graph is None or not context_id:
            return False
        from server import turn_control

        config = self._thread_config(context_id)
        try:
            async with turn_control._thread_lock(config["configurable"]["thread_id"]):
                snap = await graph.aget_state(config)
                messages = (getattr(snap, "values", None) or {}).get("messages") or []
                dangling = _dangling_tool_calls(messages)
                if dangling:
                    await self._seal_dangling_calls(graph, config, dangling)
            return True
        except Exception:  # noqa: BLE001 — best-effort hygiene; the skip stands either way
            log.exception("[interrupted-turns] could not seal dangling calls for %s", context_id)
            return False

    async def fail_task(self, task_id: str, *, reason: str = "") -> None:
        """Mark a spared-but-not-resumed task failed — the same terminal shape blanket
        reconciliation gives interactive turns, just decided after classification. The
        OCCURRENCE keeps its D7 state (``interrupted`` / ``interrupted_needs_operator``);
        this only stops the dead task handle from reading as alive forever."""
        from a2a_impl.stores import RESTART_INTERRUPTED_TEXT

        try:
            await _finalize_task(task_id, state="failed", text=reason or RESTART_INTERRUPTED_TEXT)
        except Exception:  # noqa: BLE001 — the reaper (#3418) backstops a missed write
            log.exception("[interrupted-turns] could not fail interrupted task %s", task_id)

    # ── the S7 Needs-you recovery item (Resume / Re-run / Drop) ──────────────────

    async def raise_needs_operator_decision(
        self,
        *,
        job_id: str,
        context_id: str,
        task_id: str,
        next_fire: str,
        created_at: str,
        fired_after=None,
        prompt: str = "",
    ) -> None:
        """Record the Needs-you recovery item for an occurrence disposed
        ``interrupted_needs_operator`` (ADR 0129 D7 S7): one server-recorded decision in
        the job's execution session, neutral in tone, naming the job, the step count and
        the last tool call. Options: Resume / Re-run / Drop when the checkpoint holds this
        fire's own turn; Re-run / Drop only when nothing is recoverable (no or stale
        checkpoint). Idempotent per occurrence (the ledger dedupes on the occurrence-keyed
        ``interrupt_id``), so a re-raise can never show a second card. It renders in
        Work's Needs you through the existing decision path; the answer routes back here
        (:meth:`operator_action`). Raises on a ledger failure — the scheduler's caller
        logs it and the held occurrence row stays the durable fallback."""
        from graph.decisions import decision_store, restart_recovery_interrupt_id

        from server.turn_control import _current_agent_name

        messages = []
        graph = STATE.graph
        if graph is not None and context_id:
            try:
                snap = await graph.aget_state(self._thread_config(context_id))
                messages = (getattr(snap, "values", None) or {}).get("messages") or []
            except Exception:  # noqa: BLE001 — unreadable means "no checkpoint", not a crash
                log.exception("[interrupted-turns] checkpoint unreadable for %s", context_id)
                messages = []
        anchor = _fire_anchor_index(messages, fired_after=fired_after, prompt=prompt) if messages else None
        title = _job_title(prompt, job_id)
        options = [
            {"id": RERUN_OPTION, "label": "Re-run",
             "description": "Start a fresh turn from the job's prompt."},
            {"id": DROP_OPTION, "label": "Drop",
             "description": "Settle this run as dropped without running anything."},
        ]
        if anchor is None:
            question = f"{title} was interrupted by a restart before any of this run's work was saved."
        else:
            steps, last = _turn_progress(messages, anchor)
            question = f"{title} was interrupted by a restart after {steps} step{'' if steps == 1 else 's'}."
            if last and _dangling_tool_calls(messages):
                question += f" Last action: {last}, and its effect is unknown."
            elif last:
                question += f" Last action: {last}; its result was recorded."
            options.insert(0, {
                "id": RESUME_OPTION, "label": "Resume",
                "description": "Continue from the checkpoint; the interrupted call is "
                               "reported to the agent, not re-run.",
            })
        decision_store().create(
            context_id,
            question,
            options,
            agent=_current_agent_name(),
            interrupt_id=restart_recovery_interrupt_id(job_id, next_fire, created_at, task_id),
            context=f"Recovery for scheduled job {job_id} (run due {next_fire}).",
        )
        log.warning(
            "[interrupted-turns] raised the Needs-you recovery item for job %s (task %s)", job_id, task_id
        )

    async def operator_action(
        self, rec, ref: dict, *, answer: str, answered_by: str = "operator", answered_surface: str = "console"
    ) -> None:
        """Perform the operator's answer to an S7 recovery decision — Resume / Re-run /
        Drop — exactly once (ADR 0129 D7).

        The decisions-ledger CAS is the gate: of two concurrent answers exactly one closes
        the record; the loser's :class:`DecisionClosed` is the answer route's 409. Every
        occurrence transition is itself CAS'd on the held state as the belt. The pieces
        that touch the execution session run under its turn lock: Resume drives the thread
        under it (:meth:`resume`), Re-run's hygiene seal takes it (:meth:`seal_dangling`)
        and the fresh turn it fires takes it again when the dispatch lands. Re-run is the
        ONLY path that starts a new turn, and it goes through the scheduler's one normal
        claim+fire path (new occurrence, new message identity — never a resend).

        The card parks indefinitely, so Resume must stay honest at ANY answer time: a
        checkpoint that no longer holds this fire's turn is refused BEFORE the ledger CAS
        (:class:`DecisionError` → the route's 400; the record stays pending, Re-run / Drop
        remain), and a failed handle the 24h TTL sweep already deleted is re-minted under
        the original task id (``reopen_restart_failed_task``) so the drive's real outcome
        still lands on the handle the occurrence carries."""
        from graph.decisions import DecisionClosed, DecisionError, decision_store
        from scheduler.interface import parse_iso_to_utc

        store = decision_store()
        if answer not in (RESUME_OPTION, RERUN_OPTION, DROP_OPTION):
            raise DecisionError(
                f"a recovery decision takes one of its options (Resume / Re-run / Drop), not {answer!r}"
            )
        scheduler = getattr(STATE, "scheduler", None)
        if scheduler is None:
            raise RuntimeError("scheduler unavailable; cannot act on a restart-recovery decision")
        job_id = str(ref.get("job_id") or "")
        next_fire = str(ref.get("next_fire") or "")
        created_at = str(ref.get("created_at") or "")
        claim = scheduler.interrupted_operator_claim(job_id, next_fire, created_at)
        if claim is None:
            # The occurrence moved on without this record (it should have been closed
            # with it): withdraw the stale card so it stops asking, and report conflict.
            log.warning("[interrupted-turns] recovery decision %s has no held occurrence for %s", rec.id, job_id)
            store.withdraw(rec.id, "The interrupted run was already resolved; there is nothing left to recover.")
            raise DecisionClosed(f"decision {rec.id} no longer has a held occurrence to act on")
        try:
            snapshot = json.loads(claim["job_snapshot"]) if claim["job_snapshot"] else {}
        except (TypeError, ValueError):
            snapshot = {}
        context_id = str(snapshot.get("context_id") or "")
        prompt = str(snapshot.get("prompt") or "")
        fence = list(snapshot.get("fence") or []) or None
        origin = "watch" if job_id.startswith("watch-") else "scheduler"
        task_id = str(ref.get("task_id") or claim["task_id"] or "")
        try:
            fired_after = parse_iso_to_utc(next_fire)
        except (TypeError, ValueError):
            fired_after = None

        if answer == RESUME_OPTION and await self.classify(
            context_id, fired_after=fired_after, prompt=prompt
        ) not in (CLEAN, MID_TOOL_CALL):
            # The card parks indefinitely, and Resume was offered on what the checkpoint
            # held when it was RAISED — by answer time the thread may have moved past
            # this fire (compaction, newer turns) or the checkpoint seam may be gone.
            # Refuse BEFORE the ledger CAS (no side effects yet), so the choice is never
            # consumed into a silent settled-failed: the record stays pending and the
            # operator still has Re-run / Drop.
            raise DecisionError(
                "Resume can no longer act: the checkpoint no longer holds this run's turn. "
                "Choose Re-run or Drop."
            )

        # The exactly-once gate: one concurrent answer wins this CAS, the other raises.
        store.answer(rec.id, answer, answered_by=answered_by, answered_surface=answered_surface)

        if answer == DROP_OPTION:
            if scheduler.drop_interrupted_occurrence(job_id, next_fire, created_at):
                log.warning("[interrupted-turns] operator dropped the interrupted run of %s", job_id)
            return
        if answer == RERUN_OPTION:
            # The reused session thread may still hold the dangling call; answer it with
            # the interruption report (under the session's turn lock) before any fresh
            # turn runs on it.
            await self.seal_dangling(context_id)
            if scheduler.rerun_interrupted_occurrence(job_id, next_fire, created_at):
                log.warning("[interrupted-turns] operator re-ran %s: a fresh fire is due now", job_id)
            return

        # RESUME. The disposition failed the original handle (fail_task), and the resume
        # finalizer CAS-es on the interrupted states — reopen the handle first so the
        # outcome of the drive can land on it. ``context_id`` lets the reopen RE-MINT a
        # handle the 24h TTL sweep already deleted (the card outlives the failed row;
        # the checkpoint, not the row, is what the resume drives), under the original
        # task id the occurrence has always carried.
        engine = getattr(STATE, "a2a_task_engine", None)
        reopened = False
        if engine is not None and task_id:
            from a2a_impl.stores import reopen_restart_failed_task

            reopened = await reopen_restart_failed_task(engine, task_id, context_id=context_id)
        drove = reopened and await self.resume(
            context_id,
            task_id,
            report_dangling=True,
            fired_after=fired_after,
            prompt=prompt,
            job_id=job_id,
            origin=origin,
            fence=fence,
        )
        if drove:
            # The handle now holds the turn's real outcome; normal reconciliation
            # settles the occurrence from it, exactly once.
            scheduler.release_interrupted_occurrence(
                job_id, next_fire, created_at, state="unknown",
                diagnostic="resumed by the operator after a restart interruption (ADR 0129 D7); "
                           "settling from the task handle",
            )
            return
        # The resume could not act after all: the pre-flight passed but the under-lock
        # re-check lost a race (the thread moved past this fire between the two reads),
        # or the handle could not be reopened. Never leave the owner wedged: put the
        # handle back to failed and settle the occurrence honestly — which also finishes
        # a one-shot's jobs row, like every other settle.
        if reopened:
            await self.fail_task(
                task_id, reason="Operator-ordered resume could not act; the checkpoint no longer holds this turn."
            )
        scheduler.release_interrupted_occurrence(
            job_id, next_fire, created_at, state="settled", task_state="failed",
            diagnostic="operator-ordered resume could not act (no recoverable checkpoint); "
                       "settled failed (ADR 0129 D7)",
        )
        log.error("[interrupted-turns] operator resume for %s could not act (task %s)", job_id, task_id)
