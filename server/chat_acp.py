"""ACP runtime — the per-thread coding-agent registry and turn driving (#3828, epic #3804).

Extracted from ``server/chat.py``. When ``agent_runtime: acp:<agent>`` (ADR 0033
slice 4), an external coding agent drives the turn over ACP instead of the native
LangGraph loop. This module owns:

* the **registry** — ``_ACP_RUNTIMES`` / ``_ACP_RUNTIME_ACCESS`` / ``_ACP_BUSY`` under
  ``_ACP_LOCK``, with idle-TTL + LRU-cap eviction that never closes an in-flight turn;
  this module is the ONE home of that mutable state;
* **turn driving** — ``_acp_drive_turn`` (the normalized frame stream the A2A handler
  yields) and ``_acp_turn_collected`` (the non-streaming shape);
* ``acp_sessions_snapshot`` — the read-only view behind ``GET /api/acp/sessions``.

**The ACP switch itself stays in ``server.chat``**: ``_pre_turn_dispatch`` sets
``pre.acp`` from ``is_acp_runtime`` as the last link of the shared pre-turn chain, and
both drivers call into this module through the module object (``_chat_acp.<name>``) at
call time, so a patch HERE intercepts them.

**The turn driver's collaborators live in ``server.turn_control``** (#3847). The
per-thread lock (``_thread_lock`` — its ``_THREAD_LOCKS`` registry has exactly one home)
and the thread-id resolver (``_resolve_thread_id``) are reached through that module
(``_turn_control.<name>``) at CALL time, never bound at import — a test's patch on
``server.turn_control`` is what runs, and ``import server.chat_acp`` works standalone
with no import-time edge back into ``server.chat``.

``server.chat`` re-exports every name here so ``from server.chat import
acp_sessions_snapshot`` keeps resolving. Patch (and mutate) these names HERE, not on
``server.chat``: a re-export is a copy of the binding, so a ``setattr`` there
intercepts nothing (``tests/test_chat_acp_seam.py`` enforces that).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from runtime.state import STATE
from server import turn_control as _turn_control

# Same logger as server.chat, so the moved log lines keep their channel.
log = logging.getLogger("protoagent.server")

__all__ = ["acp_sessions_snapshot"]


# One ACP runtime per thread (the ACP session is stateful — the coding agent holds
# history, so we reuse it across turns; ADR 0033 slice 4).
_ACP_RUNTIMES: dict[str, Any] = {}
_ACP_RUNTIME_ACCESS: dict[str, float] = {}  # thread_id → time.monotonic() last access
_ACP_BUSY: dict[str, int] = {}  # thread_id → in-flight turn count; never evict while > 0
_ACP_LOCK = asyncio.Lock()  # serializes registry mutation across concurrent turns
_ACP_IDLE_TTL_S = 1800  # 30 min idle before eviction
_ACP_MAX_RUNTIMES = 100  # hard cap — evict LRU when exceeded


async def _evict_acp_runtimes(now: float) -> None:
    """Sweep idle ACP runtimes and enforce the hard cap. The CALLER holds ``_ACP_LOCK``
    (kept lock-free here so it composes under the get/acquire helpers). A runtime with an
    in-flight turn (``_ACP_BUSY > 0``) is NEVER evicted — closing it would kill a live turn
    (a long ACP coding turn can outlast the idle TTL)."""
    # Phase 1 — evict entries older than the idle TTL (never an in-flight one).
    for tid in list(_ACP_RUNTIMES):
        if _ACP_BUSY.get(tid, 0) > 0:
            continue
        last = _ACP_RUNTIME_ACCESS.get(tid, 0)
        if now - last >= _ACP_IDLE_TTL_S:
            rt = _ACP_RUNTIMES.pop(tid, None)
            _ACP_RUNTIME_ACCESS.pop(tid, None)
            if rt is not None:
                await rt.close()
                log.info("[acp-runtime] evicted idle runtime for thread=%s agent=%s", tid, getattr(rt, "agent", "?"))

    # Phase 2 — over cap: evict the LRU NON-BUSY runtime until at/below the cap.
    while len(_ACP_RUNTIMES) > _ACP_MAX_RUNTIMES:
        evictable = [t for t in _ACP_RUNTIME_ACCESS if _ACP_BUSY.get(t, 0) == 0]
        if not evictable:
            break  # everything is in-flight — ride over cap rather than kill a live turn
        lru_tid = min(evictable, key=lambda k: _ACP_RUNTIME_ACCESS[k])
        rt = _ACP_RUNTIMES.pop(lru_tid, None)
        _ACP_RUNTIME_ACCESS.pop(lru_tid, None)
        if rt is not None:
            await rt.close()
            log.info("[acp-runtime] evicted LRU runtime for thread=%s agent=%s", lru_tid, getattr(rt, "agent", "?"))


async def _get_acp_runtime_locked(thread_id: str):
    """Get-or-create the runtime for ``thread_id`` (evicting idle/over-cap first). The
    CALLER holds ``_ACP_LOCK``."""
    now = time.monotonic()
    await _evict_acp_runtimes(now)
    rt = _ACP_RUNTIMES.get(thread_id)
    _ACP_RUNTIME_ACCESS[thread_id] = now  # bump on every call (hit or miss)
    if rt is None:
        from runtime.acp_runtime import AcpRuntime

        rt = AcpRuntime(STATE.graph_config)
        _ACP_RUNTIMES[thread_id] = rt
    return rt


async def _get_acp_runtime(thread_id: str):
    """Get-or-create the ACP runtime for ``thread_id`` (lock-guarded)."""
    async with _ACP_LOCK:
        return await _get_acp_runtime_locked(thread_id)


async def _acp_acquire(thread_id: str):
    """Get-or-create the runtime AND mark it in-flight (refcount++), atomically under
    ``_ACP_LOCK``, so a concurrent turn's eviction can't close it mid-turn. Pair with
    ``_acp_release`` in a ``finally``."""
    async with _ACP_LOCK:
        rt = await _get_acp_runtime_locked(thread_id)
        _ACP_BUSY[thread_id] = _ACP_BUSY.get(thread_id, 0) + 1
        return rt


async def _acp_release(thread_id: str) -> None:
    """Mark a thread's ACP runtime no longer in-flight (refcount--)."""
    async with _ACP_LOCK:
        n = _ACP_BUSY.get(thread_id, 0) - 1
        if n > 0:
            _ACP_BUSY[thread_id] = n
        else:
            _ACP_BUSY.pop(thread_id, None)
        _ACP_RUNTIME_ACCESS[thread_id] = time.monotonic()  # fresh access on release


async def acp_sessions_snapshot() -> list[dict[str, Any]]:
    """Read-only snapshot of the live ACP coding-agent registry (#2889).

    Powers ``GET /api/acp/sessions`` so the operator can triage coding
    delegation: which threads hold a runtime, which agent each speaks to, and
    whether a turn is in flight. Taken under ``_ACP_LOCK`` so a concurrent
    turn's get-or-create/evict can't tear the three registry dicts mid-read.
    Deliberately does NOT bump ``_ACP_RUNTIME_ACCESS`` — observing a session
    must not keep it alive past the idle TTL.
    """
    async with _ACP_LOCK:
        now = time.monotonic()
        return [
            {
                "thread_id": tid,
                "agent": str(getattr(rt, "agent", "?")),
                "busy": _ACP_BUSY.get(tid, 0) > 0,
                "last_access_s_ago": round(now - _ACP_RUNTIME_ACCESS.get(tid, 0.0), 1),
            }
            for tid, rt in _ACP_RUNTIMES.items()
        ]


# How long an abandoned turn gets to settle after its driver is cancelled (#3837). The
# cancel path is local: `AcpClient._prompt_locked` fences the agent's stream (sync), writes
# one `session/cancel` notification to the agent's stdin, and releases its turn lock — it
# does NOT wait for the agent's `stopReason: "cancelled"` (the fence makes the next prompt
# respawn instead of inheriting the old stream). So settling normally takes milliseconds;
# the bound exists only for a wedged stdin pipe (an agent that stopped reading) or a
# runtime that swallows the cancel, and must stay short because the caller's
# `_acp_release` — and the per-thread lock the next turn waits on — sit behind it.
_ACP_CANCEL_SETTLE_S = 10.0


async def _stop_abandoned_driver(driver: asyncio.Task, rt) -> None:
    """Cancel an abandoned turn's driver task and wait (bounded) for it to stop, so the
    caller's release happens only after the turn has actually ended. Never raises for the
    driver's own outcome; a cancel of the CALLER while waiting propagates as usual."""
    driver.cancel()
    done, _ = await asyncio.wait({driver}, timeout=_ACP_CANCEL_SETTLE_S)
    if not done:
        log.warning(
            "[acp-runtime] abandoned turn on %s did not stop within %.0fs of cancel — releasing anyway",
            getattr(rt, "agent", "?"),
            _ACP_CANCEL_SETTLE_S,
        )
        return
    if driver.cancelled():
        log.info("[acp-runtime] abandoned turn on %s cancelled (consumer went away)", getattr(rt, "agent", "?"))
    elif driver.exception() is not None:
        # Finished (with an error) before the cancel landed; retrieve it so asyncio
        # doesn't log "exception was never retrieved" — nobody is left to show it to.
        log.debug("[acp-runtime] abandoned turn ended with: %r", driver.exception())


async def _acp_drive_turn(rt, message: str):
    """Drive one ACP turn over ``rt``, yielding the normalized frames (text /
    tool_start / tool_end, then usage + done, or an error) in arrival order. Extracted
    so the A2A handler can wrap the turn in _acp_acquire/_acp_release without a deep
    in-line reindent."""
    # Bridge the agent's reader-loop callbacks (answer-text deltas + tool events) into the
    # same text / tool_start / tool_end frames the native runtime yields, in arrival order.
    _ACP_DONE = object()
    frame_q: asyncio.Queue = asyncio.Queue()

    async def _on_text(delta: str) -> None:
        await frame_q.put(("text", delta))

    async def _on_tool(ev: dict) -> None:
        if ev.get("phase") == "start":
            await frame_q.put(
                ("tool_start", {"id": ev.get("id", ""), "name": ev.get("name", "tool"), "input": ev.get("input", "")})
            )
        elif ev.get("phase") == "update":
            # The coder refined an open call's name/args (claude-agent-acp streams them
            # after the start). Re-announce the SAME id: the console fills the card in by
            # id, exactly as for the native runtime's second tool_start with full args.
            # Marked so it isn't counted as another call (#3691).
            await frame_q.put(
                (
                    "tool_start",
                    {
                        "id": ev.get("id", ""),
                        "name": ev.get("name", "tool"),
                        "input": ev.get("input", ""),
                        "refine": True,
                    },
                )
            )
        elif ev.get("phase") == "end":
            await frame_q.put(
                ("tool_end", {"id": ev.get("id", ""), "name": ev.get("name", "tool"), "output": ev.get("output", "")})
            )

    async def _drive():
        try:
            return await rt.run_turn(message, text_callback=_on_text, tool_callback=_on_tool)
        finally:
            await frame_q.put(_ACP_DONE)

    driver = asyncio.create_task(_drive())
    tool_calls = 0  # tool_start frames actually delivered to the caller (post-retry)
    finished = False
    try:
        while True:
            frame = await frame_q.get()
            if frame is _ACP_DONE:
                finished = True
                break
            if frame[0] == "tool_start" and not frame[1].get("refine"):
                tool_calls += 1
            yield frame  # (kind, payload) — already normalized
    finally:
        # The consumer walked away mid-turn (#3837): client disconnect / tab close / A2A
        # cancel land here as GeneratorExit or CancelledError at the `yield` or the
        # `get()`. Stop the turn BEFORE this generator finishes — the caller releases the
        # runtime (`_acp_release`) right after, and an orphaned driver would keep the
        # external agent working on a runtime marked idle (evictable, and the next turn
        # would queue behind a prompt nobody reads).
        if not finished:
            await _stop_abandoned_driver(driver, rt)
    try:
        answer = await driver
    except Exception as exc:  # noqa: BLE001 — surface as a turn error, don't 500
        log.exception("[acp-runtime] turn failed")
        yield ("error", f"ACP runtime ({rt.agent}) failed: {exc}")
        return
    # Boundary observability (#2991): the runtime already retries an empty reply once; if
    # what actually reached the caller is STILL empty (no tool calls + only boilerplate),
    # log it at the delivery point so the pattern is diagnosable end-to-end — the runtime's
    # own retry log and this one bracket the retry. A normal reply logs nothing.
    from runtime.acp_runtime import is_empty_delegate_reply

    if is_empty_delegate_reply(answer or "", tool_calls):
        log.warning(
            "[acp-runtime] delegate %s delivered an empty reply after retry (output_len=%d, tool_calls=%d)",
            rt.agent,
            len(answer or ""),
            tool_calls,
        )
    # Attribute the turn to the ACP agent in telemetry — gateway tokens/cost are 0 (the
    # external agent's own subscription meters its usage). The acp:<agent> model label is
    # the honest signal that this turn wasn't gateway-metered.
    usage_frame = {
        "model": f"acp:{rt.agent}",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cost_usd": 0.0,
    }
    # No context_* fields here (#3006). This used to attach `context_used_tokens` /
    # `context_window_tokens` from the runtime's ACP-native usage_update, commented
    # "so the console can render a context indicator" — but nothing ever consumed
    # them: the executor's usage handler reads a fixed key set and drops the rest,
    # and no console surface was ever built. The unit test asserted the dict this
    # function had just constructed, so it stayed green while the fields went
    # nowhere. Removed rather than wired up: ACP runtime mode is deprecated (#2548),
    # so a comment promising a shipped indicator was the actively harmful part.
    yield ("usage", usage_frame)
    # The answer already streamed as text deltas; `done` finalizes (executor appends only
    # meta when text was streamed, so no duplication).
    yield ("done", answer)


async def _acp_turn_collected(session_id: str, message: str) -> list[dict[str, Any]]:
    """The non-streaming shape of the ACP runtime switch: drive the turn, collect the
    frames, and return the single assistant message the non-streaming callers expect.
    ``usage`` is translated to the OpenAI shape the /v1 handler sums (ADR 0075 D4)."""
    tid = _turn_control._resolve_thread_id(None, session_id)
    rt = await _acp_acquire(tid)
    answer, usage, error = "", None, None
    try:
        # Same serialization as the streaming switch: one prompt per ACP session.
        async with _turn_control._thread_lock(tid):
            async for kind, payload in _acp_drive_turn(rt, message):
                if kind == "done":
                    answer = payload or answer
                elif kind == "usage" and isinstance(payload, dict):
                    _in = int(payload.get("input_tokens", 0) or 0)
                    _out = int(payload.get("output_tokens", 0) or 0)
                    usage = {"prompt_tokens": _in, "completion_tokens": _out, "total_tokens": _in + _out}
                elif kind == "error":
                    error = payload
    finally:
        await _acp_release(tid)
    # No error frame AND no `done` payload — the runtime ended the turn having said
    # nothing. Same reason as the native path's fallback (#2300): a caller must be able
    # to tell "no answer" from "an answer", and the old wording read as a deliberate
    # quiet turn rather than something to retry. Deliberately NOT the native path's exact
    # text: this path reads the answer from the runtime's `done` frame rather than by
    # scanning messages, so it never carried the stale-answer defect, and promising "this
    # is not the previous turn's answer" here would reassure against a risk that doesn't
    # exist on it.
    content = error or answer or (
        "**Error:** the ACP runtime ended the turn without a reply and without an error. "
        "Nothing was returned for this request; retry it."
    )
    out: dict[str, Any] = {"role": "assistant", "content": content}
    if usage is not None:
        out["usage"] = usage
    return [out]
