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
"""

from __future__ import annotations

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


def _fire_reached_checkpoint(messages, *, fired_after, prompt: str) -> bool:
    """Did THIS fire's prompt land in the checkpoint?

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
    skipped (fail closed). No anchor and no prompt to check against → assume reached
    (a degraded caller keeps the pre-check behavior)."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage

    from graph.compaction_engine import SENT_AT_KWARG, message_text

    want = (prompt or "").strip()
    if fired_after is None and not want:
        return True
    floor = fired_after.replace(microsecond=0) if fired_after is not None else None
    for m in messages:
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
        return True
    return False


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
                if not _fire_reached_checkpoint(messages, fired_after=fired_after, prompt=prompt):
                    # The checkpoint predates this fire (classification raced, or was
                    # skipped): nothing of this fire is recoverable — leave it for the
                    # operator rather than finish another turn under this handle.
                    return False
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
        return True

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
