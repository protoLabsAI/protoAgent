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

    async def classify(self, context_id: str) -> str:
        """``clean`` / ``mid_tool_call`` / ``no_checkpoint`` for the session's thread."""
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
        return MID_TOOL_CALL if _dangling_tool_calls(messages) else CLEAN

    async def resume(self, context_id: str, task_id: str, *, report_dangling: bool = False) -> bool:
        """Drive the thread from its checkpoint to its end — never a new prompt.

        ``report_dangling`` (the wait:/watch mid-tool-call case) first records one
        ToolMessage per dangling call — :data:`INTERRUPTED_TOOL_RESULT` — as the tools
        node's output, so the model learns the call was interrupted instead of the
        graph re-running it. The original task handle is then moved to the state the
        turn actually ended in (completed / failed / an ``input_required`` pause), so
        the scheduler's normal reconciliation settles the occurrence exactly once.

        Returns True when the thread was driven (whatever the outcome — the task
        handle now reflects it); False when nothing could be done, so the scheduler
        quarantines the occurrence for the operator instead.
        """
        graph = STATE.graph
        if graph is None or not context_id or not task_id:
            return False
        from server import turn_control

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
                dangling = _dangling_tool_calls(messages)
                if dangling:
                    if not report_dangling:
                        # Policy said resume-only-clean; the thread disagrees (it moved,
                        # or classification raced) — leave it for the operator.
                        return False
                    await self._seal_dangling_calls(graph, config, dangling)
                if dangling or getattr(snap, "next", None):
                    result = await graph.ainvoke(None, config)
                    if isinstance(result, dict) and result.get("__interrupt__"):
                        # The resumed turn parked (e.g. a decision). Hold the handle at
                        # input_required — the claim stays unresolved, so the owner
                        # admits nothing else (D6), and the pause survives TTL sweeps.
                        await _finalize_task(
                            task_id,
                            state="input_required",
                            text="Resumed after a restart and paused for input.",
                        )
                        log.warning(
                            "[interrupted-turns] resumed %s after a restart; it parked for input (task %s)",
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
