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

**Incognito (ADR 0069 D3b).** An incognito thread is never archived: the archive
would put its transcript in the knowledge store, where RAG re-injects it. With
nothing archived, the never-lossy rule refuses the manual ``/compact``
(``reason="incognito"``); the overflow safety valve still shrinks the thread,
without an archive. "Incognito" is the checkpoint's channel, so a thread is as
incognito as its latest turn — the same rule the retire harvest applies.

The archive row carries ``source=<thread_id>`` and its messages' dates (#3493,
``conversation_harvest.archive_payload``).

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

from graph.conversation_harvest import _default_summarizer, archive_payload, render_transcript

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
    force: bool = False,
) -> dict:
    """Compact ``thread_id``'s live context: archive the raw transcript, summarize,
    then rewrite the checkpoint to ``[summary, *recent_tail]``.

    Returns ``{summary, archived_chunks, kept, removed, archived, refused,
    reason}``. Honors the never-lossy invariant — a rewrite happens ONLY after the
    raw history is safely archived and a non-empty summary exists.

    ``force=True`` is the SAFETY-VALVE mode (#2783, ADR 0101 D4/D5): a context
    overflow means the thread cannot take another model call at all, so
    shrinking it outranks purity — an archive failure (or no store) proceeds
    with a loud log instead of refusing, and a summarizer failure falls back to
    a stub summary line. The manual ``/compact`` path never sets this; its
    strict never-lossy refusal stands.
    """
    if graph is None or checkpointer is None:
        return _refused("no_checkpointer", kept=0)

    lg_config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(lg_config)
    values = getattr(snapshot, "values", None) or {}
    messages = list(values.get("messages") or [])
    incognito = bool(values.get("incognito"))

    keep = keep_recent if keep_recent is not None else getattr(config, "compaction_keep_messages", _DEFAULT_KEEP_MESSAGES)
    keep = max(0, int(keep))

    # Already small enough — nothing to gain, nothing removed (not a refusal).
    if len(messages) <= keep:
        return _refused("too_short", kept=len(messages))

    # Incognito: never archived, so the manual path refuses (never-lossy); the
    # safety valve shrinks it unarchived below.
    if incognito and not force:
        return _refused("incognito", kept=len(messages))

    # Never-lossy: no archive target ⇒ never touch the checkpoint. In force
    # mode the rewrite proceeds unarchived — loudly: the thread is unusable
    # until it shrinks, and that outranks purity on the safety-valve path.
    if knowledge_store is None and not force:
        return _refused("no_store", kept=len(messages))

    import asyncio

    from knowledge import add_document

    # Archive the FULL transcript (uncapped) so the raw history is recallable —
    # a capped render would silently drop the head we're about to remove. Dated from
    # the trajectory (a file read — off the loop); messages newer than its last
    # model call take the checkpoint's own date.
    content, archive_kw = "", {}
    if incognito:
        log.warning(
            "[compact] FORCE: thread %s is incognito — shrinking it WITHOUT an archive (ADR 0069 D3b)",
            thread_id,
        )
    else:
        content, archive_kw = await asyncio.to_thread(
            archive_payload,
            messages,
            session_id=session_id,
            thread_id=thread_id,
            trailing_date=str(getattr(snapshot, "created_at", None) or "")[:10],
        )
        if not content.strip() and not force:
            # Nothing renderable to archive (e.g. an all-tool-noise thread) — refuse
            # rather than drop un-archived history.
            return _refused("empty", kept=len(messages))

    # add_document does blocking gateway work per chunk (embed + optional
    # enrichment) — keep it off the event loop (mirrors conversation_harvest).
    chunk_ids: list = []
    if knowledge_store is not None and content.strip():
        try:
            chunk_ids = await asyncio.to_thread(add_document, knowledge_store, content, **archive_kw)
        except Exception:
            if not force:
                log.exception("[compact] archive failed for thread %s — refusing to rewrite", thread_id)
                return _refused("archive_error", kept=len(messages))
            log.exception(
                "[compact] FORCE: archive failed for thread %s — compacting ANYWAY (overflow "
                "safety valve, ADR 0101 D5): the summarized-away history is NOT archived",
                thread_id,
            )
            chunk_ids = []
    if not chunk_ids and not force:
        return _refused("empty_archive", kept=len(messages))
    if not chunk_ids and force and not incognito:
        log.warning(
            "[compact] FORCE: proceeding without an archive for thread %s — the history "
            "removed by this rewrite is unrecoverable (overflow safety valve)",
            thread_id,
        )

    # Summarize the capped tail (cost-bounded classification-grade work); the head
    # beyond the cap is already archived + searchable, not lost.
    try:
        summary = (await summarizer(render_transcript(messages), config)).strip()
    except Exception:
        if not force:
            # The archive already succeeded; a summarizer failure must not 500 or leave
            # the checkpoint half-rewritten. Refuse (never-lossy) — the raw history stands
            # as a searchable archive and the live context is untouched.
            log.exception("[compact] summarize failed for thread %s — refusing to rewrite", thread_id)
            return _refused("summary_error", kept=len(messages), archived=True, archived_chunks=len(chunk_ids))
        log.exception("[compact] FORCE: summarize failed for thread %s — using a stub summary", thread_id)
        summary = ""
    if not summary:
        if not force:
            # We DID archive, but with no summary a rewrite would strip the context
            # thread — keep the full live context (archive stands as a searchable bonus).
            return _refused("no_summary", kept=len(messages), archived=True, archived_chunks=len(chunk_ids))
        # Safety valve: the thread MUST shrink. Say honestly that the summary is
        # missing rather than fake one.
        summary = (
            "(No summary is available — the earlier conversation was force-compacted after "
            "a context-window overflow."
            + (" The full transcript is archived and searchable via memory recall.)" if chunk_ids else ")")
        )

    cut = _safe_cut_index(messages, keep)
    recent_tail = messages[cut:]

    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    archived_note = (
        "the full transcript is archived and searchable via memory recall"
        if chunk_ids
        else "the removed history was NOT archived"  # force-mode honesty — never claim an archive that didn't happen
    )
    summary_msg = HumanMessage(
        content=(f"Here is a summary of the earlier conversation ({archived_note}):\n\n{summary}"),
        additional_kwargs={"lc_source": "compaction"},
    )
    await graph.aupdate_state(
        lg_config,
        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_msg, *recent_tail]},
    )
    try:
        from observability.trajectory import log_surface_op

        log_surface_op(
            thread_id,
            "compact",
            cause="overflow-force" if force else "manual",
            removed=cut,
            kept=len(recent_tail) + 1,
        )
    except Exception:  # noqa: BLE001 — trajectory is best-effort
        pass
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
        # Honest under force mode: a rewrite CAN proceed unarchived there, and
        # the result must never claim an archive that didn't happen.
        "archived": bool(chunk_ids),
        "refused": False,
        "reason": "",
    }
