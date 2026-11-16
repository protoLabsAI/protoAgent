"""Recover a retired chat's context from its durable transcript (ADR 0132 D6)."""

from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import AIMessage, HumanMessage

from runtime.state import STATE

log = logging.getLogger("protoagent.server")

#: The task store persists proto-JSON state names (``a2a.types.TaskState`` keys — see
#: ``_PRESERVED_STATES`` in ``a2a_impl/stores.py``), NOT the lowercase wire aliases. A
#: finished turn is in one of these terminal states; recovery seeds only from them so an
#: in-flight/paused turn (``TASK_STATE_WORKING`` / ``…_INPUT_REQUIRED``) is never a prior turn.
_TERMINAL_STATES = ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED")

#: Persisted history messages carry proto-JSON role names, not lowercase aliases.
_ROLE_USER = "ROLE_USER"
_ROLE_AGENT = "ROLE_AGENT"

#: Set on a task row's ``metadata`` by :func:`mark_turns_rewound` when an operator discards
#: its turn via rewind. Recovery skips flagged rows, so a pruned chat never re-injects turns
#: the operator rolled the agent's live context back past. A task-level flag (not a history
#: edit) keeps the transcript the console rebuilds (GET …/turns) untouched.
_REWOUND_FLAG = "protoagent_rewound"
_REWOUND_PATH = f"$.{_REWOUND_FLAG}"


async def recovered_messages(config: dict, session_id: str, *, incognito: bool = False) -> list:
    """Seed only absent checkpoints; callers hold the thread lock through invocation.

    Return input messages rather than writing a separate checkpoint: the graph
    commits the recovered context with the new turn.
    In-flight tasks (including this request and queued requests) are not prior turns.
    Fresh-context goal iterations deliberately start empty.
    """
    engine = getattr(STATE, "a2a_task_engine", None)
    tid = config["configurable"]["thread_id"]
    if engine is None or not session_id or incognito or ":goal-iter-" in tid:
        return []
    snapshot = await STATE.graph.aget_state(config)
    existing = (snapshot.values or {}).get("messages", [])
    # The turn-level error handler can write a failure note when a recovery read
    # fails. That note must not permanently prevent recovery on the next attempt.
    only_failures = bool(existing) and all(
        isinstance(m, AIMessage) and m.additional_kwargs.get("protoagent_turn_failed") for m in existing
    )
    if not only_failures and (
        (snapshot.config or {}).get("configurable", {}).get("checkpoint_id") or snapshot.values
    ):
        return []

    from a2a.server.tasks.database_task_store import TaskModel
    from sqlalchemy import func, select

    from a2a_impl.stores import task_newest_first

    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(TaskModel.history, TaskModel.artifacts)
                .where(TaskModel.context_id == session_id)
                .where(TaskModel.status["state"].as_string().in_(_TERMINAL_STATES))
                # Turns the operator discarded via rewind (graph/rewind_op) are flagged in
                # place, not deleted — exclude them here so the limit counts live turns only.
                .where(func.json_extract(TaskModel.task_metadata, _REWOUND_PATH).is_(None))
                .order_by(*task_newest_first(TaskModel, conn.dialect.name))
                .limit(20)
            )
        ).fetchall()

    messages = []
    for history, artifacts in reversed(rows):
        history = history or []
        if any((m.get("metadata") or {}).get(k) for m in history for k in ("incognito", "hidden")):
            continue
        for msg in history:
            text = _parts_text(msg)
            if text and msg.get("role") in (_ROLE_USER, _ROLE_AGENT):
                cls = HumanMessage if msg["role"] == _ROLE_USER else AIMessage
                messages.append(cls(content=text))
        answer = "\n\n".join(filter(None, (_parts_text(a) for a in artifacts or [])))
        if answer and not (messages and isinstance(messages[-1], AIMessage) and messages[-1].content == answer):
            messages.append(AIMessage(content=answer))
    if not messages:
        return []

    store = STATE.history_store
    if store is not None:
        entries = await asyncio.to_thread(store.list_for_session, session_id, kinds=("summary",))
        summary = next((e.content for e in reversed(entries) if e.thread_id == tid and e.content.strip()), "")
        if summary:
            messages.insert(0, HumanMessage(content=f"[Summary of this conversation before checkpoint retirement]\n{summary}"))
    return messages


async def mark_turns_rewound(context_id: str, *, content: str | None, occurrence: int = 0, before: bool = False) -> int:
    """Flag the stored turns an operator discarded via rewind so recovery skips them.

    ``rewind_thread`` (graph/rewind_op) rewrites only the LangGraph checkpoint — the durable
    A2A task rows stay, because the transcript every device rebuilds from (GET …/turns) keeps
    the full record. Once the age sweep deletes that checkpoint, :func:`recovered_messages`
    reads those rows; without this mark it would re-inject turns the operator rolled the
    agent's memory back past.

    Resolves the target the way the console sends it — the visible bubble ``content`` and its
    ``occurrence`` across both roles, in chronological order — against the stored turns, then
    flags every turn created AFTER it (``before`` flags the target turn too, matching
    Regenerate's exclusive cut). Returns the number of turns flagged.

    Best-effort: the checkpoint rewind has already succeeded, so a resolve miss (a non-content
    target, or text that no stored bubble carries) or any store error only degrades a later
    recovery — it never fails the rewind. Runs under the caller's per-thread lock.
    """
    engine = getattr(STATE, "a2a_task_engine", None)
    want = (content or "").strip()
    if engine is None or not context_id or not want:
        return 0
    try:
        from a2a.server.tasks.database_task_store import TaskModel
        from sqlalchemy import literal_column, select, update

        rowid = literal_column("tasks.rowid")
        async with engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(rowid, TaskModel.history, TaskModel.artifacts)
                    .where(TaskModel.context_id == context_id)
                    .order_by(rowid.asc())
                )
            ).fetchall()
            seen, cut_rid = 0, None
            for rid, history, artifacts in rows:
                for bubble in _turn_bubbles(history, artifacts):
                    if bubble == want:
                        if seen == occurrence:
                            cut_rid = rid
                            break
                        seen += 1
                if cut_rid is not None:
                    break
            if cut_rid is None:
                return 0
            # before=False keeps THROUGH the target turn; before=True (Regenerate) drops it too.
            after = rowid >= cut_rid if before else rowid > cut_rid
            to_flag = (
                await conn.execute(
                    select(TaskModel.id, TaskModel.task_metadata).where(TaskModel.context_id == context_id).where(after)
                )
            ).fetchall()
            flagged = 0
            for tid, md in to_flag:
                merged = dict(md or {})
                if merged.get(_REWOUND_FLAG):
                    continue
                merged[_REWOUND_FLAG] = True
                await conn.execute(update(TaskModel).where(TaskModel.id == tid).values(task_metadata=merged))
                flagged += 1
            return flagged
    except Exception:  # noqa: BLE001 — best-effort; the rewind itself already landed.
        log.warning("[rehydrate] could not flag rewound turns for %s", context_id, exc_info=True)
        return 0


def _turn_bubbles(history, artifacts):
    """The content bubbles of one stored turn, chronologically — the operator's prompt(s)
    then the turn's answer — mirroring the console bubbles a rewind ``occurrence`` counts.
    Intermediate agent frames (tool calls, reasoning) are tool cards, not content bubbles,
    so they're skipped here even though :func:`recovered_messages` still feeds them to the
    model."""
    for msg in history or []:
        if msg.get("role") == _ROLE_USER and (text := _parts_text(msg).strip()):
            yield text
    answer = "\n\n".join(filter(None, (_parts_text(a).strip() for a in artifacts or [])))
    if answer:
        yield answer


def _parts_text(message: dict) -> str:
    return "".join(p["text"] for p in message.get("parts") or [] if isinstance(p.get("text"), str))
