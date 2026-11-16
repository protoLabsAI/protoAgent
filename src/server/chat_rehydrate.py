"""Recover a retired chat's context from its durable transcript (ADR 0132 D6)."""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage, HumanMessage

from runtime.state import STATE


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
    from sqlalchemy import select

    from a2a_impl.stores import task_newest_first

    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(TaskModel.history, TaskModel.artifacts)
                .where(TaskModel.context_id == session_id)
                .where(TaskModel.status["state"].as_string().in_(("completed", "failed", "canceled", "rejected")))
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
            if text and msg.get("role") in ("user", "agent"):
                cls = HumanMessage if msg["role"] == "user" else AIMessage
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


def _parts_text(message: dict) -> str:
    return "".join(p["text"] for p in message.get("parts") or [] if isinstance(p.get("text"), str))
