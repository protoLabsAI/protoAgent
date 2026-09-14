"""Harvest a retired conversation into the searchable knowledge base.

When a chat thread is retired — aged out by the checkpoint pruner, or explicitly
deleted — we don't just drop it: we summarize it and ingest the summary into the
``KnowledgeStore`` (FTS5 + embeddings), so the substance becomes searchable via
``memory_recall`` while the bulky raw checkpoints are reclaimed. Save space,
keep the signal.

The summary is produced by the cheap aux model (``routing.aux_model``) — it's
classification-grade work, not the main reasoning task.
"""

from __future__ import annotations

import logging
import re

from langchain_core.messages import AIMessage, HumanMessage

from graph.output_format import extract_output

log = logging.getLogger(__name__)

# Cap the transcript fed to the summarizer (keep the most recent tail).
_MAX_TRANSCRIPT_CHARS = 16000


def render_transcript(messages: list, *, max_chars: int | None = _MAX_TRANSCRIPT_CHARS) -> str:
    """Render a User/Assistant transcript from checkpoint messages.

    Assistant turns are run through ``extract_output`` (drop scratch_pad/think);
    tool and system messages are skipped. Truncated to the most-recent
    ``max_chars`` when long; pass ``max_chars=None`` for the full transcript (the
    compaction path archives the *whole* conversation losslessly before it
    rewrites the live context — a capped render would silently drop the head).
    """
    lines = [line for line in (_line(m) for m in messages) if line is not None]
    transcript = "\n".join(lines)
    if max_chars is not None and len(transcript) > max_chars:
        transcript = "…\n" + transcript[-max_chars:]
    return transcript


def _line(m, day: str = "") -> str | None:
    """One transcript line (``User: …`` / ``Assistant: …``), or None for a message the
    transcript skips. ``day`` stamps the line with when the message was sent."""
    content = getattr(m, "content", "")
    if not isinstance(content, str) or not content.strip():
        return None
    tag = f" [{day}]" if day else ""
    if isinstance(m, HumanMessage):
        return f"User{tag}: {content.strip()}"
    if isinstance(m, AIMessage):
        clean = extract_output(content).strip()
        return f"Assistant{tag}: {clean}" if clean else None
    return None


_DAY = re.compile(r"\d{4}-\d{2}-\d{2}$")


def _today() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date().isoformat()


def message_dates(messages: list, session_id: str, *, trailing_date: str = "") -> dict[str, str]:
    """``{message id: YYYY-MM-DD}``: the day each message was first sent to the model.

    Checkpoint messages carry no timestamp of their own, so this reads the session's
    trajectory (ADR 0102), which logs every model call's message ids. ``trailing_date``
    dates the messages AFTER the last one the trajectory has seen (they arrived after
    the last model call). It is applied only when at least one message IS dated: a
    session without a trajectory stays undated instead of looking brand new, which is
    the mistake this exists to stop (#3493). Never raises."""
    ids = [str(getattr(m, "id", None) or "") for m in messages]
    wanted = {i for i in ids if i}
    if not wanted or not session_id:
        return {}
    try:
        from observability.trajectory import trajectory_log

        seen = trajectory_log.first_seen(session_id, wanted)
    except Exception:  # noqa: BLE001 — dating is best-effort; an archive is still written
        seen = {}
    dates = {i: ts[:10] for i, ts in seen.items() if _DAY.match(ts[:10])}
    if dates and _DAY.match(trailing_date or ""):
        last = max(k for k, i in enumerate(ids) if i in dates)
        for i in ids[last + 1 :]:
            if i:
                dates.setdefault(i, trailing_date)
    return dates


def archive_payload(
    messages: list,
    *,
    session_id: str,
    thread_id: str | None,
    cause: str = "",
    trailing_date: str | None = None,
) -> tuple[str, dict]:
    """The compaction archive write: ``(content, add_document kwargs)``, shared by
    auto-compaction and ``/compact``. ``content`` is ``""`` when nothing renders.

    Provenance and dates (#3493). An archive used to carry only the compaction day
    (``created_at``) and no ``source``, so a transcript from weeks earlier was recalled
    as current. Now the row's ``source`` is the thread it came from, the heading names
    the span of the messages' dates, and the content opens with that span. Each line also
    carries its own day, so every chunk the store splits the transcript into still says
    when it was written: recall shows a chunk's text and stored date, never its heading.

    ``trailing_date`` defaults to today (see :func:`message_dates`)."""
    today = _today()
    trailing = trailing_date if _DAY.match(trailing_date or "") else today
    dates = message_dates(messages, session_id, trailing_date=trailing)
    lines: list[str] = []
    days: list[str] = []
    undated = False
    for m in messages:
        day = dates.get(str(getattr(m, "id", None) or ""), "")
        line = _line(m, day)
        if line is None:
            continue
        lines.append(line)
        if day:
            days.append(day)
        else:
            undated = True
    transcript = "\n".join(lines)
    if not transcript.strip():
        return "", {}
    if days:
        first, last = min(days), max(days)
        span = first if first == last else f"{first} to {last}"
        lead = f"[Conversation archive: messages from {span}" + ("; some messages undated" if undated else "")
        label = f"messages {span}"
    else:
        lead = "[Conversation archive: message dates unknown"
        label = f"archived {today}"
    lead += f"; archived {today}]"
    prefix = f"{cause}, " if cause else ""
    return f"{lead}\n{transcript}", {
        "domain": "conversation",
        "heading": f"Conversation archive ({prefix}{session_id}, {label})",
        # source=<thread> is the provenance link the harvest already writes (ADR 0069 D5).
        "source": thread_id or None,
        # Agent-derived trust tier (ADR 0069 D8): the operator's own conversation.
        "source_type": "conversation",
        "namespace": f"chat-archive:{session_id}",
    }


# What "forget what this chat saved" removes by ``source`` — the harvest's summaries
# and extracted facts. NOT "conversation": memory_ingest writes that type with the
# session as its source, and a fork's thread-id resolver may BE the session id.
# Compaction archives are reached through their namespace instead.
_FORGET_SOURCE_TYPES = ("harvest", "extracted")


def forget_conversation_memory(knowledge_store, session_id: str, thread_ids) -> int:
    """Delete what a chat already wrote to the knowledge store (#3493, the delete
    dialog's opt-in). Returns the number of rows removed.

    Exactly two kinds of row:

    - its compaction archives: everything in ``chat-archive:<session_id>``
      (auto-compaction and ``/compact``, with or without a ``source``);
    - summaries and facts harvested from one of its threads: ``source`` equal to
      one of ``thread_ids`` (or one of their ``:goal-iter-N`` sub-threads, which the
      TTL sweep can harvest on their own), ``source_type`` harvest/extracted.

    Out of reach, by design or by history: memories the agent was asked to keep
    (``memory_ingest``, hot memory), background-job reports, and facts stored before
    provenance existed (``source="harvest"``, which names no thread). A fact from
    ANOTHER chat that one of this chat's facts superseded stays superseded.

    Hard delete, like the chat delete it rides on. Stores without the method (a plugin
    backend) are skipped with a warning rather than failing the delete."""
    if knowledge_store is None or not session_id:
        return 0
    removed = 0
    by_namespace = getattr(knowledge_store, "delete_by_namespace", None)
    if callable(by_namespace):
        removed += int(by_namespace(f"chat-archive:{session_id}") or 0)
    else:
        log.warning("[forget] knowledge store has no delete_by_namespace — archives of %s kept", session_id)
    by_source = getattr(knowledge_store, "delete_by_source", None)
    if callable(by_source):
        for tid in dict.fromkeys(str(t) for t in thread_ids if t):
            removed += int(by_source(tid, source_types=_FORGET_SOURCE_TYPES) or 0)
            removed += int(by_source(f"{tid}:goal-iter-", source_types=_FORGET_SOURCE_TYPES, prefix=True) or 0)
    else:
        log.warning("[forget] knowledge store has no delete_by_source — harvested rows of %s kept", session_id)
    log.info("[forget] removed %d knowledge row(s) written by session %s", removed, session_id)
    return removed


_SUMMARY_PROMPT = (
    "Summarize this chat conversation for long-term, searchable memory. Capture "
    "the user's goals, the concrete facts/preferences they shared, decisions "
    "made, and outcomes — anything worth recalling in a future conversation. "
    "Write a concise factual summary (a few sentences). Omit pleasantries and "
    "meta-commentary.\n\nConversation:\n{transcript}\n\nSummary:"
)


async def _default_summarizer(transcript: str, config) -> str:
    from graph.agent import _resolve_aux_model
    from graph.llm import create_llm

    llm = create_llm(config, model_name=_resolve_aux_model(config, ""))
    resp = await llm.ainvoke([HumanMessage(content=_SUMMARY_PROMPT.format(transcript=transcript))])
    # The aux model may or may not wrap output in tags; extract defensively.
    return extract_output(str(resp.content)).strip() or str(resp.content).strip()


def _as_of(tup) -> str:
    """``YYYY-MM-DD`` of the thread's last checkpoint (its last activity), or ``""``."""
    ts = str(((getattr(tup, "checkpoint", None) or {}).get("ts")) or "")
    return ts[:10] if re.match(r"\d{4}-\d{2}-\d{2}", ts) else ""


async def harvest_thread(
    thread_id: str,
    *,
    checkpointer,
    knowledge_store,
    config,
    summarizer=_default_summarizer,
    namespace: str | None = None,
    fact_extractor=None,
    raise_on_error: bool = False,
) -> str | None:
    """Retire ``thread_id``'s conversation into the knowledge base (ADR 0021).

    The single session-end pass: store an **episodic** summary
    (``domain="conversation"``) and, when ``config.knowledge_facts``, also
    extract **semantic** facts (``finding_type="fact"``) and consolidate them.
    Both carry ``namespace`` for later per-project scoping.

    Returns the summary chunk id, or None when there's nothing to harvest (no
    store, no checkpoint, incognito thread — ADR 0069 D3b, empty transcript,
    or a summarizer failure). Best-effort by default (a failure logs and returns
    None); ``raise_on_error=True`` re-raises instead, so a caller that must tell
    FAILURE from legitimately-nothing (#2946: the retire path) can.
    """
    if knowledge_store is None:
        return None
    # Background worker thread (ADR 0070 D3): its transcript is disposable — the
    # report was already delivered to (and indexed under) the ORIGIN session at
    # completion, so harvesting the worker would duplicate the report into the KB
    # under the worker's identity. Mirrors the incognito skip below; string-matched
    # so legacy retired threads are covered too.
    if thread_id.startswith("background:") or ":background:" in thread_id:
        log.info("[harvest] thread %s is a background worker — skipping harvest", thread_id)
        return None
    try:
        tup = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None:
            return None
        channel_values = (tup.checkpoint or {}).get("channel_values", {})
        # Incognito thread (ADR 0069 D3b): "no memory trail" must hold at
        # retirement too — without this gate the retire sweep (harvest_enabled
        # defaults ON) would summarize the transcript into the knowledge store,
        # where RAG re-injects it into later prompts. Same per-message
        # semantics as _persist_session: the channel holds the last stamped
        # value, so a thread is as incognito as its latest turn.
        if channel_values.get("incognito"):
            log.info("[harvest] thread %s is incognito — skipping harvest", thread_id)
            return None
        messages = channel_values.get("messages", [])
        transcript = render_transcript(messages)
        if not transcript.strip():
            return None
        summary = await summarizer(transcript, config)
        if not summary.strip():
            return None
        # Date what we store to when the conversation happened, not when it retired. The
        # TTL sweep retires threads weeks after their last turn. Undated, an August
        # "the user runs model X" landed as a present-tense fact stamped with the harvest
        # day (2026-09-10) and was recalled as the current setup.
        as_of = _as_of(tup)
        if as_of:
            summary = f"[as of {as_of}] {summary}"
        # A summary is document-sized — chunk it so each passage gets its own
        # embedding instead of one diluted whole-summary vector (ADR 0021).
        # Offloaded: add_document does blocking gateway work per chunk (embed +
        # optional contextual enrichment) — keep it off the maintenance loop.
        import asyncio

        from knowledge import add_document

        # source=<thread_id> is the machine-readable provenance link (ADR 0069
        # D5) — the heading carries it for humans, but recall/audit key on the
        # row's source column. source_type="harvest" ranks the rows in the
        # agent-derived trust tier (ADR 0069 D8).
        chunk_ids = await asyncio.to_thread(
            add_document,
            knowledge_store,
            summary,
            domain="conversation",
            heading=f"Conversation summary ({thread_id}" + (f", last active {as_of})" if as_of else ")"),
            source=thread_id,
            source_type="harvest",
            namespace=namespace,
        )
        chunk_id = chunk_ids[0] if chunk_ids else None
        log.info(
            "[harvest] summarized thread %s into knowledge (%d chunk(s), first %s)",
            thread_id,
            len(chunk_ids),
            chunk_id,
        )

        # Semantic facts — the second half of the session-end pass (ADR 0021).
        if getattr(config, "knowledge_facts", False):
            from graph.memory_facts import extract_and_store_facts

            kwargs = {
                "knowledge_store": knowledge_store,
                "config": config,
                "namespace": namespace,
                "source": thread_id,
                "as_of": as_of,
            }
            if fact_extractor is not None:
                kwargs["extractor"] = fact_extractor
            await extract_and_store_facts(transcript, **kwargs)

        return chunk_id
    except Exception:
        log.exception("[harvest] failed for thread %s", thread_id)
        # `None` also means "legitimately nothing to harvest" (no store, incognito,
        # background worker, empty transcript) — a FAILURE is only distinguishable when
        # the caller opts into the raise (#2946: the retire path must not delete a
        # thread whose harvest failed transiently, so it needs to tell the two apart).
        if raise_on_error:
            raise
        return None
