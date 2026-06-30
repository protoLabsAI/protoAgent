"""SessionSummaryMiddleware — persists a session summary on each terminal turn.

Writes a reasoning-stripped JSON summary of the session to disk (``memory_path()``)
on the terminal turn and on session end, enabling cross-session memory across
restarts — read back by ``KnowledgeMiddleware`` as a ``<prior_sessions>`` block.

It does **not** write to the knowledge store: the old per-turn finding extraction
was removed in ADR 0021 (it dumped raw, truncated, scratch_pad-laden turns). KB
capture now lives in ``conversation_harvest`` (on thread retire) + the fact
extractor — extract, don't dump.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DISABLE_ENV = os.environ.get("PROTOAGENT_DISABLE_MEMORY", "")
_PERSISTENCE_DISABLED = _DISABLE_ENV.lower() in ("1", "true", "yes")

if _PERSISTENCE_DISABLED:
    log.debug("[memory] persistence disabled via PROTOAGENT_DISABLE_MEMORY")
else:
    log.info("[memory] session persistence enabled")


def memory_path() -> str:
    """The session-memory dir, resolved lazily on each call — NOT an import-time
    constant (env identity is finalized after this module imports).

    ``MEMORY_PATH`` env wins (verbatim); else the per-instance ``instance_root/memory``
    store. The old literal ``/sandbox/memory`` silently skipped persistence on any
    non-container host (read-only ``/``); the instance store is always writable."""
    raw = os.environ.get("MEMORY_PATH", "").strip()
    if raw:
        return str(Path(raw).expanduser())
    from infra.paths import instance_paths

    return str(instance_paths().store("memory"))


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def _persist_session(state: dict, trace_id: str) -> None:
    """Write a session summary JSON file atomically.

    Summary schema:
        session_id       — str
        trace_id         — str
        messages         — list[{"role": str, "content": str}]
        tool_calls       — top-5 by duration list[{"name", "args", "result", "duration_ms"}]
        tool_calls_total_count — int (present when > 5 tool calls)
        final_output     — str | null
        timestamp        — ISO-8601 UTC string

    Writes atomically: temp file → os.rename to avoid partial reads.
    """
    if _PERSISTENCE_DISABLED:
        return

    # ``session_id`` is not a declared graph-state field, so LangGraph drops the
    # key the chat path passes into ``ainvoke`` — ``state.get`` returns "" and
    # every session would collapse into a single ``unknown.json`` (pooling and
    # cross-contaminating sessions). Fall back to the tracing contextvar, which
    # ``trace_session`` always sets, so summaries are keyed per session.
    session_id: str = state.get("session_id", "") or ""
    if not session_id:
        from observability import tracing

        session_id = tracing.current_session_id() or ""
    messages_raw: list = state.get("messages", []) or []

    # --- Extract user-visible messages ---
    # Assistant content is run through strip_reasoning so the session file (later
    # injected as <prior_sessions>) never carries the model's <scratch_pad> —
    # the ADR 0021 never-persist-reasoning rule applied to this path too.
    from graph.output_format import strip_reasoning

    user_messages: list[dict] = []
    for msg in messages_raw:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            user_messages.append({"role": "user", "content": content})
        elif isinstance(msg, AIMessage) and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            user_messages.append({"role": "assistant", "content": strip_reasoning(content)})

    # --- Extract tool call records ---
    # Reconstruct from AI messages (which carry tool_calls) and ToolMessages
    tool_results: dict[str, str] = {}
    all_tool_calls: list[dict] = []

    for msg in messages_raw:
        if isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            tool_results[tool_call_id] = msg.content if isinstance(msg.content, str) else str(msg.content)

    for msg in messages_raw:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                all_tool_calls.append(
                    {
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                        "result": tool_results.get(tc_id, ""),
                        "duration_ms": 0,  # timing not available in state
                    }
                )

    total_count = len(all_tool_calls)

    # Top-5 by duration (duration is 0 for all when not available — stable sort)
    sorted_calls = sorted(all_tool_calls, key=lambda x: x["duration_ms"], reverse=True)
    top_calls = sorted_calls[:5]

    # --- Final output: last assistant message ---
    final_output: str | None = None
    for msg in reversed(messages_raw):
        if isinstance(msg, AIMessage) and msg.content:
            raw_final = msg.content if isinstance(msg.content, str) else str(msg.content)
            final_output = strip_reasoning(raw_final)
            break

    # --- Build summary ---
    summary: dict[str, Any] = {
        "session_id": session_id,
        "trace_id": trace_id,
        "messages": user_messages,
        "tool_calls": top_calls,
        "final_output": final_output,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if total_count > 5:
        summary["tool_calls_total_count"] = total_count

    # --- Ensure directory exists ---
    base = memory_path()
    try:
        os.makedirs(base, exist_ok=True)
        log.debug("[memory] ensured directory: %s", base)
    except OSError as exc:
        log.warning("[memory] cannot create directory %s: %s — skipping persistence", base, exc)
        return

    # --- Atomic write ---
    filename = f"{session_id or 'unknown'}.json"
    dest = os.path.join(base, filename)
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=base, suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2, default=str)
            tmp_fd = None  # fdopen took ownership
        os.rename(tmp_path, dest)
        log.info("[memory] persisted session %s -> %s", session_id, dest)
        tmp_path = None  # rename succeeded — no cleanup needed
    except OSError as exc:
        log.error("[memory] write failed for session %s: %s", session_id, exc)
    finally:
        # Clean up temp file if rename didn't happen
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Prior-sessions loader — single source of truth (ADR 0021)
# ---------------------------------------------------------------------------


def load_prior_sessions(
    memory_dir: str | None = None,
    max_sessions: int = 10,
    max_tokens: int = 2000,
) -> str:
    """Format the most-recent persisted sessions as a ``<prior_sessions>`` block.

    The canonical loader used by *both* ``SessionSummaryMiddleware`` and
    ``KnowledgeMiddleware`` — previously two copy-pasted implementations. Reads
    up to ``max_sessions`` newest JSON files, drops oldest-first to fit
    ``max_tokens`` (char/4 approximation), and **strips reasoning at read** so a
    file written before the persist-time strip (or by an older build) still
    can't inject ``<scratch_pad>`` into the prompt. ``memory_dir`` defaults to the
    writer's resolved ``memory_path()``. Never raises.
    """
    from graph.output_format import strip_reasoning

    if memory_dir is None:
        memory_dir = memory_path()
    if not os.path.isdir(memory_dir):
        return ""
    try:
        entries: list[tuple[float, str]] = []
        for fname in os.listdir(memory_dir):
            if not fname.endswith(".json"):
                continue
            fpath = os.path.join(memory_dir, fname)
            try:
                entries.append((os.path.getmtime(fpath), fpath))
            except OSError:
                continue
        entries.sort(reverse=True)  # newest first
    except OSError:
        return ""
    if not entries:
        return "<prior_sessions/>"

    summaries: list[dict] = []
    for _, fpath in entries[:max_sessions]:
        try:
            with open(fpath, encoding="utf-8") as fh:
                summaries.append(json.load(fh))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    if not summaries:
        return "<prior_sessions/>"

    def _format(s: dict) -> str:
        ts = s.get("timestamp", "unknown")
        sid = s.get("session_id", "unknown")
        lines = [f'<session id="{sid}" timestamp="{ts}">']
        msgs = s.get("messages", []) or []
        if msgs:
            lines.append("  <messages>")
            for m in msgs:
                role = m.get("role", "unknown")
                content = strip_reasoning(m.get("content", "") or "")[:500]
                lines.append(f"    <{role}>{content}</{role}>")
            lines.append("  </messages>")
        final = strip_reasoning(s.get("final_output") or "")[:300]
        if final:
            lines.append(f"  <final_output>{final}</final_output>")
        lines.append("</session>")
        return "\n".join(lines)

    formatted = [_format(s) for s in summaries]
    while formatted:
        if max(1, len("\n".join(formatted)) // 4) <= max_tokens:
            break
        formatted.pop()  # drop oldest (newest-first ordering)
    if not formatted:
        return "<prior_sessions/>"
    return "<prior_sessions>\n" + "\n".join(formatted) + "\n</prior_sessions>"


# ---------------------------------------------------------------------------
# Middleware class
# ---------------------------------------------------------------------------


class SessionSummaryMiddleware(AgentMiddleware):
    """Persist a session summary on the terminal turn (+ on session end).

    Writes a reasoning-stripped JSON summary to ``memory_path()``, read back by
    ``KnowledgeMiddleware`` as ``<prior_sessions>`` for cross-session continuity.

    **Write-only.** It does not write to the knowledge store (ADR 0021 — see
    ``after_agent``) and does not inject ``<prior_sessions>``: that read/inject
    path is owned solely by ``KnowledgeMiddleware``, so cross-session continuity
    requires the knowledge middleware (on by default).
    """

    def __init__(self, knowledge_store=None):
        super().__init__()
        # Accepted for ctor compatibility; unused now that this is write-only.
        self._store = knowledge_store

    def after_agent(self, state, runtime) -> dict | None:
        """Persist a session summary on the terminal turn.

        Knowledge capture is **not** done here. The per-turn ``add_finding``
        dump that used to live here stored raw assistant turns — scratch_pad and
        all, truncated mid-content — which the retrieval layer then recycled into
        future prompts. ADR 0021 removed it: conversation knowledge is captured
        by ``conversation_harvest`` (summarized, scratch_pad-stripped) when a
        thread retires, and semantic facts by the extractor — extract, don't
        dump; background, not hot-path.
        """
        messages = state.get("messages", [])

        # Session persistence: terminal = last message is an AIMessage with
        # content and no pending tool calls.
        if messages:
            last_msg = messages[-1]
            if isinstance(last_msg, AIMessage) and last_msg.content and not getattr(last_msg, "tool_calls", None):
                from observability import tracing

                trace_id = tracing.current_trace_id()
                _persist_session(state, trace_id)
        return None

    async def aafter_agent(self, state, runtime) -> dict | None:
        return self.after_agent(state, runtime)

    # --- Session persistence ---

    def on_session_end(self, state, runtime) -> dict | None:
        """Persist session summary to disk when session reaches terminal state."""
        from observability import tracing

        trace_id = tracing.current_trace_id()
        _persist_session(state, trace_id)
        return None

    async def aon_session_end(self, state, runtime) -> dict | None:
        return self.on_session_end(state, runtime)
