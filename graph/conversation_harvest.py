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

from langchain_core.messages import AIMessage, HumanMessage

from graph.output_format import extract_output

log = logging.getLogger(__name__)

# Cap the transcript fed to the summarizer (keep the most recent tail).
_MAX_TRANSCRIPT_CHARS = 16000


def render_transcript(messages: list) -> str:
    """Render a User/Assistant transcript from checkpoint messages.

    Assistant turns are run through ``extract_output`` (drop scratch_pad/think);
    tool and system messages are skipped. Returns the most-recent
    ``_MAX_TRANSCRIPT_CHARS`` when long.
    """
    lines: list[str] = []
    for m in messages:
        content = getattr(m, "content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        if isinstance(m, HumanMessage):
            lines.append(f"User: {content.strip()}")
        elif isinstance(m, AIMessage):
            clean = extract_output(content).strip()
            if clean:
                lines.append(f"Assistant: {clean}")
    transcript = "\n".join(lines)
    if len(transcript) > _MAX_TRANSCRIPT_CHARS:
        transcript = "…\n" + transcript[-_MAX_TRANSCRIPT_CHARS:]
    return transcript


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


async def harvest_thread(
    thread_id: str,
    *,
    checkpointer,
    knowledge_store,
    config,
    summarizer=_default_summarizer,
) -> str | None:
    """Summarize ``thread_id``'s conversation into the knowledge base.

    Returns the new chunk id, or None when there's nothing to harvest (no
    knowledge store, no checkpoint, empty transcript, or a summarizer failure).
    Never raises — harvesting is best-effort and must not block retirement.
    """
    if knowledge_store is None:
        return None
    try:
        tup = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
        if tup is None:
            return None
        messages = (tup.checkpoint or {}).get("channel_values", {}).get("messages", [])
        transcript = render_transcript(messages)
        if not transcript.strip():
            return None
        summary = await summarizer(transcript, config)
        if not summary.strip():
            return None
        chunk_id = knowledge_store.add_chunk(
            summary,
            domain="conversation",
            heading=f"Conversation summary ({thread_id})",
        )
        log.info("[harvest] summarized thread %s into knowledge (chunk %s)", thread_id, chunk_id)
        return chunk_id
    except Exception:
        log.exception("[harvest] failed for thread %s", thread_id)
        return None
