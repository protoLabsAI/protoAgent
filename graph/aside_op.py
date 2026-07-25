"""`/btw` — an incognito side turn overlaid on the current chat's context (#2180).

Ask the agent a quick question *about* the ongoing conversation without it becoming
part of that conversation: the side turn SEES the main thread's context and answers,
but the main thread is never written to and the exchange leaves no memory trail.

**The isolation is the whole feature, so it's structural, not hoped-for.** The side turn
runs on a SEPARATE, ephemeral thread id — the main thread's checkpoint is only ever READ
(`aget_state`), never opened for write. That's the difference from "just set
`incognito=True` on the current thread": incognito (ADR 0069) suppresses the *memory*
trail (no harvest, no injection) but the turn still checkpoints the thread it runs on — so
running the aside on the main thread would leak the side chat into the real history. Here
the main thread is untouched by construction, and `incognito=True` additionally keeps the
side turn out of memory. Both halves, belt and suspenders.

The ephemeral thread's own checkpoint (seeded context + the aside Q&A) is deleted after the
turn — best-effort cleanup, so a side chat leaves nothing behind even on disk.

Host-free-ish: takes the graph + checkpointer + thread id like `export_op` / `rewind_op`,
so the isolation invariant is unit-testable against a fake graph with no server.
"""

from __future__ import annotations

import logging
import secrets

log = logging.getLogger(__name__)


def _answer_from(values: dict) -> str:
    """The last AI message's text from a finished graph state."""
    from langchain_core.messages import AIMessage

    for m in reversed(list((values or {}).get("messages") or [])):
        if isinstance(m, AIMessage):
            content = getattr(m, "content", "")
            if isinstance(content, list):  # multi-part → join text blocks
                content = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            text = str(content).strip()
            if text:
                return text
    return ""


async def run_aside(
    graph,
    checkpointer,
    thread_id: str,
    question: str,
    *,
    session_id: str = "",
    db_path: str | None = None,
) -> dict:
    """Run one incognito side turn seeded with ``thread_id``'s current context.

    Returns ``{found, answer, reason}``. ``found`` is false (no turn run) when there's no
    graph/checkpointer or the question is empty. **Never writes the main thread's
    checkpoint** — it reads the messages, then runs on a fresh ephemeral thread. The
    ephemeral thread is deleted afterwards (best-effort) so nothing is left on disk.
    """
    if graph is None or checkpointer is None:
        return {"found": False, "answer": "", "reason": "no_checkpointer"}
    if not (question or "").strip():
        return {"found": False, "answer": "", "reason": "empty_question"}

    from langchain_core.messages import HumanMessage

    # 1. READ the main thread's context — the only touch of the main thread, read-only.
    main_cfg = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(main_cfg)
    context = list((getattr(snapshot, "values", None) or {}).get("messages") or [])

    # 2. Run the side turn on an EPHEMERAL thread seeded with that context, incognito.
    aside_tid = f"{thread_id}::aside-{secrets.token_hex(6)}"
    aside_cfg = {"configurable": {"thread_id": aside_tid}}
    graph_input = {
        "messages": [*context, HumanMessage(content=question)],
        "session_id": session_id,
        "incognito": True,  # no memory harvest/injection on top of the thread isolation
    }
    try:
        result = await graph.ainvoke(graph_input, config=aside_cfg)
        answer = _answer_from(result)
    finally:
        # 3. Drop the ephemeral thread's checkpoint — the side chat leaves no disk trace.
        if db_path:
            try:
                from graph.checkpoint_prune import delete_thread

                delete_thread(db_path, aside_tid, cascade=True)
            except Exception as e:  # noqa: BLE001 — cleanup is best-effort; isolation already holds
                log.info("[aside] ephemeral checkpoint cleanup skipped: %s", e)

    return {"found": True, "answer": answer or "(no answer)", "reason": "ok"}
