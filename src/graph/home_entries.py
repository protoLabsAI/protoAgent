"""Home entries: owner notices written into Home's own thread (ADR 0135 D9, ADR 0129 D6
amendment, #5545).

A brief or an alert that reaches the operator is also appended to the Home conversation's
checkpoint thread as ONE reference message, so a reply in Home sees it and the browser is
not the only copy. :func:`write_home_entry` does the append; :func:`post_home_entry` is the
synchronous, best-effort front the notice seam (``notify_chat_surfaces``) calls from any
thread.

* **Never a turn.** The entry is a checkpoint write (``graph.aupdate_state``), the same
  out-of-band append ``server.chat.record_directive_withdrawal`` uses. Nothing is invoked.
* **Not the operator's words.** The message carries :data:`HOME_ENTRY_KWARG`, which
  ``compaction_engine.human_kind`` reads as system-written, so a notice's text is never
  taken for an operator instruction.
* **Serialized with Home's turns.** The append takes the per-thread turn lock, so it cannot
  lost-update a running Home turn. The lock registry lives in ``server.turn_control``, which
  this package must not import; ``server/__init__`` hands it in (:func:`set_thread_lock`).
* **A parked Home is left alone.** A fresh message on a thread paused at an approval or a
  form abandons that pause, so the entry waits in memory and is retried until Home is free.
  Waiting entries are bounded and are lost on a restart (the notice itself is retained on
  the bus and delivered by its adapters either way).
* **Flag.** With the ``home_conversation`` developer flag off, nothing is written.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from typing import Any

log = logging.getLogger("protoagent.home")

#: Opening of every Home entry's text, so the model reads it as a reference, not a request.
HOME_ENTRY_PREFIX = "[Home entry"

MAX_TITLE_CHARS = 300
MAX_BODY_CHARS = 4000
_PENDING_MAX = 100
_RETRY_SECONDS = 30.0

# (thread_id) -> async context manager; ``server.turn_control._thread_lock`` once wired.
_thread_lock: Callable[[str], AbstractAsyncContextManager] | None = None
# Entries waiting for a parked Home thread, oldest first.
_pending: deque = deque(maxlen=_PENDING_MAX)
_retry_task: asyncio.Task | None = None
# Strong references to in-flight writes scheduled by ``post_home_entry``.
_inflight: set = set()


def set_thread_lock(lock: Callable[[str], AbstractAsyncContextManager] | None) -> None:
    """Hand in the per-thread turn lock (``server.turn_control._thread_lock``)."""
    global _thread_lock
    _thread_lock = lock


def _enabled() -> bool:
    from runtime.flags import flag_enabled
    from runtime.home_session import FLAG_ID

    return flag_enabled(FLAG_ID)


def is_operator_chat(session_id: str | None) -> bool:
    """Whether ``session_id`` is an operator chat (Home included): the ``chat-`` sessions the
    console lists. What is raised there is already in front of the operator."""
    return str(session_id or "").startswith("chat-")


def _clip(text: object, limit: int) -> str:
    text = str(text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _message(
    kind: str, importance: str, title: str, source: str, notice_id: str | None, body: str | None,
    meta: dict | None = None,
):
    from langchain_core.messages import HumanMessage

    from graph.compaction_engine import HOME_ENTRY_KWARG, sent_at_kwargs

    title = _clip(title, MAX_TITLE_CHARS)
    body = _clip(body, MAX_BODY_CHARS)
    head = f"{HOME_ENTRY_PREFIX}: {kind} from {source or 'notice'}, importance {importance}. For reference; not a request.]"
    return HumanMessage(
        content="\n\n".join(part for part in (f"{head}\n{title}", body) if part),
        # A stable id per notice: writing the same notice twice replaces, never duplicates.
        id=f"home-entry-{notice_id or uuid.uuid4().hex}",
        additional_kwargs={
            **sent_at_kwargs(),
            HOME_ENTRY_KWARG: {
                # A caller's extra keys first: they never replace the four core keys below.
                **(meta or {}),
                "kind": kind,
                "importance": importance,
                "source": source,
                "notice_id": notice_id or None,
            },
        },
    )


def _interrupt_pending(snapshot) -> bool:
    """Whether the thread is paused for a human (mirrors ``server.chat._pending_interrupt``)."""
    for task in getattr(snapshot, "tasks", ()) or ():
        if getattr(task, "result", None) is None and (getattr(task, "interrupts", ()) or ()):
            return True
    return bool(getattr(snapshot, "interrupts", None))


async def _append(messages: list) -> bool:
    """Append ``messages`` to Home's thread. ``False`` when Home is paused for a human and
    the write must wait. Raises when there is no graph or the checkpoint write fails."""
    from langgraph.errors import InvalidUpdateError
    from langgraph.graph import START

    from graph.thread_ids import resolve_thread_id
    from runtime.home_session import home_session_id
    from runtime.state import STATE

    graph = STATE.graph
    if graph is None:
        raise RuntimeError("no agent graph is running")
    session_id = await asyncio.to_thread(home_session_id)  # a file read; keep it off the loop
    thread_id = resolve_thread_id(None, session_id)
    config = {"configurable": {"thread_id": thread_id}}
    async with _thread_lock(thread_id) if _thread_lock is not None else nullcontext():
        if _interrupt_pending(await graph.aget_state(config)):
            return False
        update = {"messages": list(messages)}
        try:
            # No ``as_node`` first: on a finished thread this leaves it finished.
            await graph.aupdate_state(config, update)
        except InvalidUpdateError:
            # LangGraph could not tell which node the update follows; record it as input.
            await graph.aupdate_state(config, update, as_node=START)
    return True


async def _retry_pending() -> None:
    """Write the waiting entries, in order, once Home is no longer paused."""
    global _retry_task
    try:
        while _pending:
            await asyncio.sleep(_RETRY_SECONDS)
            batch = list(_pending)
            try:
                if not await _append(batch):
                    continue
            except Exception:  # noqa: BLE001 — a failed write is dropped, not retried forever
                log.warning("[home] could not write %d waiting Home entries", len(batch), exc_info=True)
            for msg in batch:
                try:
                    _pending.remove(msg)
                except ValueError:  # already pushed out by the bound
                    pass
    finally:
        _retry_task = None


def _defer(msg) -> None:
    global _retry_task
    _pending.append(msg)
    if _retry_task is None or _retry_task.done():
        _retry_task = asyncio.get_running_loop().create_task(_retry_pending())


async def write_home_entry(
    kind: str,
    importance: str,
    title: str,
    source: str,
    notice_id: str | None = None,
    body: str | None = None,
    meta: dict | None = None,
) -> bool:
    """Append one reference message to Home's thread; never starts a turn.

    The message's ``additional_kwargs[HOME_ENTRY_KWARG]`` is ``{kind, importance, source,
    notice_id}``; ``notice_id`` is the key a push click uses to find the entry. ``meta`` adds
    the caller's own keys to that dict (a scheduled result's ``activity_context`` and
    ``task_id``); it cannot replace the four core keys, and without it the dict is exactly
    those four. Returns
    ``True`` once written. Returns ``False`` when the Home flag is off (nothing happens) or
    when Home is paused for a human, in which case the entry waits and is written, in
    arrival order, after the pause clears. Raises if the write itself fails; callers on the
    notice path go through :func:`post_home_entry`, which logs instead.
    """
    if not _enabled():
        return False
    msg = _message(kind, importance, title, source, notice_id, body, meta)
    if not _pending and await _append([msg]):
        return True
    _defer(msg)  # behind anything already waiting, so entries keep their order
    return False


async def _write_logged(**entry: Any) -> None:
    try:
        await write_home_entry(**entry)
    except Exception:  # noqa: BLE001 — a Home entry is best-effort
        log.warning("[home] could not write the Home entry for notice %s", entry.get("notice_id"), exc_info=True)


def post_home_entry(
    kind: str,
    importance: str,
    title: str,
    source: str,
    notice_id: str | None = None,
    body: str | None = None,
    meta: dict | None = None,
) -> bool:
    """Queue :func:`write_home_entry` on the server loop without waiting for it.

    Safe from the server loop and from a store's worker thread. Best-effort: with the flag
    off, with no loop to run on, or on any failure it does nothing and never raises.
    Returns whether a write was queued.
    """
    try:
        if not _enabled():
            return False
        from runtime.state import STATE

        entry = {
            "kind": kind, "importance": importance, "title": title, "source": source,
            "notice_id": notice_id, "body": body, "meta": dict(meta) if meta else None,
        }
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = getattr(STATE, "main_loop", None)
        if loop is None or not loop.is_running():
            loop = running
        if loop is None:
            log.debug("[home] no server loop; skipped the Home entry for notice %s", notice_id)
            return False
        if loop is running:
            task = loop.create_task(_write_logged(**entry))
        else:
            task = asyncio.run_coroutine_threadsafe(_write_logged(**entry), loop)
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
        return True
    except Exception:  # noqa: BLE001 — never stop the bus publish or adapter dispatch
        log.warning("[home] could not queue the Home entry for notice %s", notice_id, exc_info=True)
        return False
