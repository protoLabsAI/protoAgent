"""On-demand conversation compaction — the ``/compact`` operator gesture (#1527).

This is the *manual*, whole-thread analogue of the automatic
``SummarizationMiddleware`` (``graph/middleware/compaction.py``): instead of
waiting for the context window to fill, an operator asks for a compaction now.
The live LangGraph checkpoint is the agent's *real* context, so a client-only
compaction would do nothing — this runs SERVER-SIDE against the checkpointer.

The pass, for one thread:

1. ``aget_state`` the current messages off the checkpoint.
2. Render the **full** transcript and archive it into the searchable knowledge
   store (``domain="conversation"``, ``namespace="chat-archive:<session_id>"``)
   so nothing is lost — the raw history stays recallable via ``memory_recall``.
3. Summarize the conversation with the cheap aux model.
4. Rewrite the checkpoint to ``[RemoveMessage(REMOVE_ALL_MESSAGES), summary,
   *recent_tail]`` via ``aupdate_state`` — so the next turn carries the whole
   thread's context at a fraction of the token cost.

**Never-lossy (hard invariant).** Compaction must never drop history it could
not archive. If there is no knowledge store, or the archive write yields no
chunks, or the summarizer produces nothing, we DO NOT touch the checkpoint and
return ``refused=True`` — the operator keeps their full, intact context.

**Message-boundary integrity (hard invariant).** The recent tail must never
orphan a ``ToolMessage`` from the ``AIMessage(tool_calls=…)`` that spawned it —
the next model call errors ("tool_call without response"). We reuse the same
safe-cut the auto-summarizer uses: if the naive cutoff lands on a
``ToolMessage``, walk back to include its parent ``AIMessage`` (so the pair is
kept together), summarizing slightly more rather than splitting a pair.

Host-free and unit-testable: it takes the graph, checkpointer, knowledge store,
and config as arguments (no ``STATE`` import), mirroring
``conversation_harvest.harvest_thread``.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from graph.conversation_harvest import _default_summarizer, render_transcript

log = logging.getLogger(__name__)

_DEFAULT_KEEP_MESSAGES = 20


def _safe_cut_index(messages: list, keep_last: int) -> int:
    """Index where the retained tail should start so it keeps at least
    ``keep_last`` messages WITHOUT orphaning a ``ToolMessage`` from its parent
    ``AIMessage``.

    Mirrors ``SummarizationMiddleware._find_safe_cutoff`` /
    ``_find_safe_cutoff_point``: land ``keep_last`` from the end, then, if that
    lands on a ``ToolMessage``, walk *back* to the ``AIMessage`` whose
    ``tool_calls`` produced it so the tool request/response pair stays together
    (falling forward past the tool block only if no parent is found). Returns 0
    when everything fits — keep it all.
    """
    n = len(messages)
    if keep_last <= 0:
        target = n  # keep nothing but the summary
    elif n <= keep_last:
        return 0
    else:
        target = n - keep_last

    if target >= n or not isinstance(messages[target], ToolMessage):
        return target

    # target sits on a ToolMessage — gather the ids of the consecutive tool block
    # and search backward for the AIMessage that requested them.
    tool_call_ids: set[str] = set()
    idx = target
    while idx < n and isinstance(messages[idx], ToolMessage):
        tcid = getattr(messages[idx], "tool_call_id", None)
        if tcid:
            tool_call_ids.add(tcid)
        idx += 1
    for i in range(target - 1, -1, -1):
        m = messages[i]
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            ai_ids = {tc.get("id") for tc in m.tool_calls if tc.get("id")}
            if tool_call_ids & ai_ids:
                return i
    # No matching AIMessage (edge case) — fall forward past the orphan tool block.
    return idx


def _refused(reason: str, *, kept: int, archived: bool = False, archived_chunks: int = 0, summary: str = "") -> dict:
    """A no-rewrite result. ``refused`` is the never-lossy signal (the caller must
    NOT drop client history); ``too_short`` is a benign no-op, not a refusal."""
    return {
        "summary": summary,
        "archived_chunks": archived_chunks,
        "kept": kept,
        "removed": 0,
        "archived": archived,
        "refused": reason not in ("", "too_short"),
        "reason": reason,
    }


async def compact_thread(
    graph,
    checkpointer,
    knowledge_store,
    config,
    thread_id: str,
    session_id: str,
    *,
    summarizer=_default_summarizer,
    keep_recent: int | None = None,
) -> dict:
    """Compact ``thread_id``'s live context: archive the raw transcript, summarize,
    then rewrite the checkpoint to ``[summary, *recent_tail]``.

    Returns ``{summary, archived_chunks, kept, removed, archived, refused,
    reason}``. Honors the never-lossy invariant — a rewrite happens ONLY after the
    raw history is safely archived and a non-empty summary exists.
    """
    if graph is None or checkpointer is None:
        return _refused("no_checkpointer", kept=0)

    lg_config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(lg_config)
    messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])

    keep = keep_recent if keep_recent is not None else getattr(config, "compaction_keep_messages", _DEFAULT_KEEP_MESSAGES)
    keep = max(0, int(keep))

    # Already small enough — nothing to gain, nothing removed (not a refusal).
    if len(messages) <= keep:
        return _refused("too_short", kept=len(messages))

    # Never-lossy: no archive target ⇒ never touch the checkpoint.
    if knowledge_store is None:
        return _refused("no_store", kept=len(messages))

    # Archive the FULL transcript (uncapped) so the raw history is recallable —
    # a capped render would silently drop the head we're about to remove.
    full_transcript = render_transcript(messages, max_chars=None)
    if not full_transcript.strip():
        # Nothing renderable to archive (e.g. an all-tool-noise thread) — refuse
        # rather than drop un-archived history.
        return _refused("empty", kept=len(messages))

    import asyncio

    from knowledge import add_document

    # add_document does blocking gateway work per chunk (embed + optional
    # enrichment) — keep it off the event loop (mirrors conversation_harvest).
    try:
        chunk_ids = await asyncio.to_thread(
            add_document,
            knowledge_store,
            full_transcript,
            domain="conversation",
            heading=f"Conversation archive ({session_id})",
            namespace=f"chat-archive:{session_id}",
        )
    except Exception:
        log.exception("[compact] archive failed for thread %s — refusing to rewrite", thread_id)
        return _refused("archive_error", kept=len(messages))
    if not chunk_ids:
        return _refused("empty_archive", kept=len(messages))

    # Summarize the capped tail (cost-bounded classification-grade work); the head
    # beyond the cap is already archived + searchable, not lost.
    try:
        summary = (await summarizer(render_transcript(messages), config)).strip()
    except Exception:
        # The archive already succeeded; a summarizer failure must not 500 or leave
        # the checkpoint half-rewritten. Refuse (never-lossy) — the raw history stands
        # as a searchable archive and the live context is untouched.
        log.exception("[compact] summarize failed for thread %s — refusing to rewrite", thread_id)
        return _refused("summary_error", kept=len(messages), archived=True, archived_chunks=len(chunk_ids))
    if not summary:
        # We DID archive, but with no summary a rewrite would strip the context
        # thread — keep the full live context (archive stands as a searchable bonus).
        return _refused("no_summary", kept=len(messages), archived=True, archived_chunks=len(chunk_ids))

    cut = _safe_cut_index(messages, keep)
    recent_tail = messages[cut:]

    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    summary_msg = HumanMessage(
        content=(
            "Here is a summary of the earlier conversation "
            "(the full transcript is archived and searchable via memory recall):\n\n"
            f"{summary}"
        ),
        additional_kwargs={"lc_source": "compaction"},
    )
    await graph.aupdate_state(
        lg_config,
        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_msg, *recent_tail]},
    )
    log.info(
        "[compact] thread %s: archived %d chunk(s), removed %d msg(s), kept %d",
        thread_id,
        len(chunk_ids),
        cut,
        len(recent_tail),
    )
    return {
        "summary": summary,
        "archived_chunks": len(chunk_ids),
        "kept": len(recent_tail),
        "removed": cut,
        "archived": True,
        "refused": False,
        "reason": "",
    }
