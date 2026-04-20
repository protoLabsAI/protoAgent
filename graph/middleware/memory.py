"""MemoryMiddleware — queues conversation for async knowledge extraction.

After the agent responds, extracts key topics/findings from the
conversation and stores them in the knowledge base asynchronously.

Also persists a session summary to disk when sessions end via the
on_session_end lifecycle hook, enabling session memory across restarts.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from langgraph.prebuilt.chat_agent_executor import AgentState


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — read once at module init
# ---------------------------------------------------------------------------

MEMORY_PATH = os.environ.get("MEMORY_PATH", "/sandbox/memory/")
_DISABLE_ENV = os.environ.get("PROTOAGENT_DISABLE_MEMORY", "")
_PERSISTENCE_DISABLED = _DISABLE_ENV.lower() in ("1", "true", "yes")

if _PERSISTENCE_DISABLED:
    log.debug("[memory] persistence disabled via PROTOAGENT_DISABLE_MEMORY")
else:
    log.info("[memory] session persistence enabled — path: %s", MEMORY_PATH)


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

    session_id: str = state.get("session_id", "") or ""
    messages_raw: list = state.get("messages", []) or []

    # --- Extract user-visible messages ---
    user_messages: list[dict] = []
    for msg in messages_raw:
        if isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            user_messages.append({"role": "user", "content": content})
        elif isinstance(msg, AIMessage) and msg.content:
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            user_messages.append({"role": "assistant", "content": content})

    # --- Extract tool call records ---
    # Reconstruct from AI messages (which carry tool_calls) and ToolMessages
    tool_results: dict[str, str] = {}
    all_tool_calls: list[dict] = []

    for msg in messages_raw:
        if isinstance(msg, ToolMessage):
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            tool_results[tool_call_id] = (
                msg.content if isinstance(msg.content, str) else str(msg.content)
            )

    for msg in messages_raw:
        if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                tc_id = tc.get("id", "")
                all_tool_calls.append({
                    "name": tc.get("name", ""),
                    "args": tc.get("args", {}),
                    "result": tool_results.get(tc_id, ""),
                    "duration_ms": 0,  # timing not available in state
                })

    total_count = len(all_tool_calls)

    # Top-5 by duration (duration is 0 for all when not available — stable sort)
    sorted_calls = sorted(all_tool_calls, key=lambda x: x["duration_ms"], reverse=True)
    top_calls = sorted_calls[:5]

    # --- Final output: last assistant message ---
    final_output: str | None = None
    for msg in reversed(messages_raw):
        if isinstance(msg, AIMessage) and msg.content:
            final_output = msg.content if isinstance(msg.content, str) else str(msg.content)
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
    try:
        os.makedirs(MEMORY_PATH, exist_ok=True)
        log.debug("[memory] ensured directory: %s", MEMORY_PATH)
    except OSError as exc:
        log.warning("[memory] cannot create directory %s: %s — skipping persistence", MEMORY_PATH, exc)
        return

    # --- Atomic write ---
    filename = f"{session_id or 'unknown'}.json"
    dest = os.path.join(MEMORY_PATH, filename)
    tmp_fd = None
    tmp_path = None
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(dir=MEMORY_PATH, suffix=".tmp")
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
# Middleware class
# ---------------------------------------------------------------------------

class MemoryMiddleware(AgentMiddleware):
    """Extract and store QA findings after agent responses.

    Also persists a session summary on session end via on_session_end.
    """

    def __init__(self, knowledge_store=None):
        super().__init__()
        self._store = knowledge_store
        self._prior_sessions_cache: str | None = None

    # --- Session memory loading (only used when no KnowledgeMiddleware is active) ---

    def _load_prior_sessions(self) -> str:
        """Lazy-load prior session summaries when standalone (no KnowledgeMiddleware).

        When KnowledgeMiddleware is also in the chain it owns `<prior_sessions>`
        injection. This method runs only when `self._store is None`, so there is
        no double-injection risk.

        Reads from MEMORY_PATH, returns an XML block or empty string on first
        run. Mirrors KnowledgeMiddleware.load_memory() but without the store
        dependency — single source of truth would be cleaner but would couple
        the two files.
        """
        if not os.path.isdir(MEMORY_PATH):
            return ""
        try:
            entries = []
            for fname in os.listdir(MEMORY_PATH):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(MEMORY_PATH, fname)
                try:
                    entries.append((os.path.getmtime(fpath), fpath))
                except OSError:
                    continue
            entries.sort(reverse=True)
        except OSError:
            return ""
        if not entries:
            return "<prior_sessions/>"
        summaries = []
        for _, fpath in entries[:10]:
            try:
                with open(fpath, encoding="utf-8") as fh:
                    summaries.append(json.load(fh))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        if not summaries:
            return "<prior_sessions/>"
        lines_out = []
        for s in summaries:
            ts = s.get("timestamp", "unknown")
            sid = s.get("session_id", "unknown")
            lines = [f'<session id="{sid}" timestamp="{ts}">']
            msgs = s.get("messages", []) or []
            if msgs:
                lines.append("  <messages>")
                for m in msgs:
                    role = m.get("role", "unknown")
                    content = (m.get("content", "") or "")[:500]
                    lines.append(f"    <{role}>{content}</{role}>")
                lines.append("  </messages>")
            final = (s.get("final_output") or "")[:300]
            if final:
                lines.append(f"  <final_output>{final}</final_output>")
            lines.append("</session>")
            lines_out.append("\n".join(lines))
        # 2K token budget — chars // 4 approx, drop oldest first
        while lines_out:
            joined = "\n".join(lines_out)
            if max(1, len(joined) // 4) <= 2000:
                break
            lines_out.pop()
        if not lines_out:
            return "<prior_sessions/>"
        return "<prior_sessions>\n" + "\n".join(lines_out) + "\n</prior_sessions>"

    def before_model(self, state, runtime) -> dict | None:
        """Inject `<prior_sessions>` into system prompt when running standalone.

        When KnowledgeMiddleware is present it handles this; we only act when
        `self._store is None`.
        """
        if self._store is not None:
            return None
        if self._prior_sessions_cache is None:
            self._prior_sessions_cache = self._load_prior_sessions()
        if not self._prior_sessions_cache:
            return None
        messages = state.get("messages", [])
        if not messages:
            return None
        # Prepend as a system-adjacent HumanMessage block. LangGraph has no
        # dedicated system-context append hook on state, so we piggyback on
        # the first human message by modifying its content.
        from langchain_core.messages import SystemMessage
        first = messages[0]
        if isinstance(first, SystemMessage):
            # Already has a system message — append prior_sessions to it
            new_content = first.content + "\n\n" + self._prior_sessions_cache
            new_msgs = [SystemMessage(content=new_content)] + list(messages[1:])
            return {"messages": new_msgs}
        # Otherwise prepend a new SystemMessage
        new_msgs = [SystemMessage(content=self._prior_sessions_cache)] + list(messages)
        return {"messages": new_msgs}

    async def abefore_model(self, state, runtime) -> dict | None:
        return self.before_model(state, runtime)

    # --- Knowledge extraction (existing) ---

    def after_agent(self, state, runtime) -> dict | None:
        """Queue conversation for async knowledge extraction. Persists session on terminal turn."""
        messages = state.get("messages", [])

        # --- Session persistence: detect terminal turn ---
        # Terminal = last message is AIMessage with content and no pending tool calls
        if messages:
            last_msg = messages[-1]
            if (
                isinstance(last_msg, AIMessage)
                and last_msg.content
                and not getattr(last_msg, "tool_calls", None)
            ):
                import tracing
                trace_id = tracing.current_trace_id()
                _persist_session(state, trace_id)

        # --- Knowledge extraction (only when a store is configured) ---
        if self._store is None:
            return None
        if len(messages) < 2:
            return None

        # Extract the last exchange (human + AI)
        last_human = None
        last_ai = None
        for msg in reversed(messages):
            if isinstance(msg, AIMessage) and last_ai is None:
                last_ai = msg.content if isinstance(msg.content, str) else str(msg.content)
            elif isinstance(msg, HumanMessage) and last_human is None:
                last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
            if last_human and last_ai:
                break

        if not last_human or not last_ai:
            return None

        # Only store if the response contains substantial content
        if len(last_ai) < 100:
            return None

        # Async storage — don't block the response
        def _store():
            try:
                self._store.add_finding(
                    content=last_ai[:2000],
                    source="conversation",
                    source_type="chat",
                    finding_type="insight",
                )
            except Exception:
                pass

        threading.Thread(target=_store, daemon=True).start()
        return None

    async def aafter_agent(self, state, runtime) -> dict | None:
        return self.after_agent(state, runtime)

    # --- Session persistence ---

    def on_session_end(self, state, runtime) -> dict | None:
        """Persist session summary to disk when session reaches terminal state."""
        import tracing
        trace_id = tracing.current_trace_id()
        _persist_session(state, trace_id)
        return None

    async def aon_session_end(self, state, runtime) -> dict | None:
        return self.on_session_end(state, runtime)
