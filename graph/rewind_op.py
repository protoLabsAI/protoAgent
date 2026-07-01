"""On-demand conversation rewind — the "Rewind to here" operator gesture (#1535).

The destructive sibling of ``compaction_op`` (the ``/compact`` gesture): instead
of summarizing older history to save tokens, the operator points at a message and
says "discard everything after this." The live LangGraph checkpoint is the agent's
*real* context, so a client-only truncate would leave the agent's memory intact —
this runs SERVER-SIDE against the checkpointer and rewrites the message log.

The pass, for one thread:

1. ``aget_state`` the current messages off the checkpoint.
2. Locate the target message (by raw index, by message ``id``, or — the console
   path — by matching its rendered ``content``, since the client's message ids are
   client-generated and never appear in the checkpoint).
3. Keep the prefix THROUGH the target, then rewrite the checkpoint to
   ``[RemoveMessage(REMOVE_ALL_MESSAGES), *kept_prefix]`` via ``aupdate_state``.

**Rewind is intentionally destructive** — unlike compaction it is NOT never-lossy:
the messages after the cut are meant to be thrown away (there is no archive). What
it must NOT do is *corrupt* the log.

**Message-boundary integrity (hard invariant).** The kept prefix must never end on
an ``AIMessage(tool_calls=…)`` whose ``ToolMessage`` responses fall past the cut —
that leaves an orphaned tool_call and the next model call errors ("tool_call
without response"). We reuse the same safe-cut idea as the auto-summarizer /
``compaction_op._safe_cut_index``: if the naive cut lands inside a tool-call block,
extend FORWARD to pull the answering ``ToolMessage``\\s back in, and only if that
can't balance, fall BACK to before the requesting ``AIMessage``.

Host-free and unit-testable: it takes the graph + checkpointer + thread id as
arguments (no ``STATE`` import), mirroring ``compaction_op.compact_thread``.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, ToolMessage

log = logging.getLogger(__name__)


def _open_tool_calls(messages: list) -> set[str]:
    """The tool_call ids REQUESTED by ``AIMessage.tool_calls`` in ``messages`` that
    have no answering ``ToolMessage`` in the same slice — i.e. the tool calls a
    kept prefix would orphan."""
    opened: set[str] = set()
    answered: set[str] = set()
    for m in messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                tid = tc.get("id")
                if tid:
                    opened.add(tid)
        elif isinstance(m, ToolMessage):
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                answered.add(tcid)
    return opened - answered


def _safe_cut_end(messages: list, end: int) -> int:
    """Adjust ``end`` (an EXCLUSIVE cut — the kept prefix is ``messages[:end]``) so
    it never orphans a ``ToolMessage`` from the ``AIMessage(tool_calls=…)`` that
    spawned it.

    The prefix-side analogue of ``compaction_op._safe_cut_index``: if the naive cut
    would leave a tool-call block half-kept, first extend FORWARD over the answering
    ``ToolMessage``\\s (keeping the request/response pair together, discarding
    slightly less), and only if that still can't balance, fall BACK before the
    requesting ``AIMessage`` (dropping the unanswered request). Returns ``end``
    unchanged when the prefix is already balanced — including the keep-nothing /
    keep-everything ends, which can't orphan anything.
    """
    n = len(messages)
    end = max(0, min(end, n))
    if end == 0 or end == n:
        return end
    # Forward: while the kept prefix has unanswered tool calls and the very next
    # message is a ToolMessage answering the block, pull it in.
    while end < n and isinstance(messages[end], ToolMessage) and _open_tool_calls(messages[:end]):
        end += 1
    # Back: if the prefix is still unbalanced (couldn't answer forward), retreat
    # before the requesting AIMessage(s) until nothing is orphaned.
    while end > 0 and _open_tool_calls(messages[:end]):
        end -= 1
    return end


def _resolve_end(messages: list, *, target_index, target_id, target_content, occurrence=None) -> int | None:
    """Index of the message JUST PAST the target (the naive, pre-safe-cut prefix
    length), or ``None`` if the target can't be located.

    Precedence: an explicit ``target_index`` wins; then a matching message ``id``;
    then the LAST message whose ``content`` equals ``target_content`` (the console
    path — the visible assistant bubble's text matches its final ``AIMessage``).
    Last-occurrence is the conservative pick when identical replies repeat.
    """
    n = len(messages)
    if target_index is not None:
        idx = int(target_index)
        if idx < 0:
            idx += n  # allow -1 = last
        if 0 <= idx < n:
            return idx + 1
        return None
    if target_id is not None:
        for i, m in enumerate(messages):
            if getattr(m, "id", None) == target_id:
                return i + 1
        # fall through to content matching if an id was given but not found
    if target_content is not None:
        want = str(target_content).strip()
        if want:
            matches = [i for i in range(n) if str(getattr(messages[i], "content", "") or "").strip() == want]
            if matches:
                # The client sends WHICH occurrence of this content it clicked — identical
                # replies can repeat, and picking the last match would silently keep a LATER
                # duplicate the user meant to discard (defeating the whole point of rewind).
                # Pick that same occurrence from the start; fall back to the last match
                # (conservative — discards less, never corrupts) only when unaligned.
                if occurrence is not None and 0 <= int(occurrence) < len(matches):
                    return matches[int(occurrence)] + 1
                return matches[-1] + 1
    return None


async def rewind_thread(
    graph,
    checkpointer,
    thread_id: str,
    *,
    target_index: int | None = None,
    target_id: str | None = None,
    target_content: str | None = None,
    occurrence: int | None = None,
) -> dict:
    """Rewind ``thread_id``'s live context to the target message: keep the prefix
    through it and discard everything after, rewriting the checkpoint in place.

    Returns ``{found, kept, removed, reason}``. ``found`` is false (with no rewrite)
    when there's no checkpointer or the target can't be located; ``removed == 0`` is
    a benign no-op (the target was already the last message). Boundary integrity is
    enforced via ``_safe_cut_end`` — a rewind never leaves an orphaned tool_call.
    """
    if graph is None or checkpointer is None:
        return {"found": False, "kept": 0, "removed": 0, "reason": "no_checkpointer"}

    lg_config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(lg_config)
    messages = list((getattr(snapshot, "values", None) or {}).get("messages") or [])

    raw_end = _resolve_end(
        messages,
        target_index=target_index,
        target_id=target_id,
        target_content=target_content,
        occurrence=occurrence,
    )
    if raw_end is None:
        return {"found": False, "kept": len(messages), "removed": 0, "reason": "not_found"}

    end = _safe_cut_end(messages, raw_end)
    kept = messages[:end]
    removed = len(messages) - end

    if removed <= 0:
        # Target is already the tail — nothing to discard. Don't touch the checkpoint.
        return {"found": True, "kept": len(kept), "removed": 0, "reason": "noop"}

    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    await graph.aupdate_state(
        lg_config,
        {"messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *kept]},
    )
    log.info("[rewind] thread %s: kept %d msg(s), discarded %d", thread_id, len(kept), removed)
    return {"found": True, "kept": len(kept), "removed": removed, "reason": ""}
