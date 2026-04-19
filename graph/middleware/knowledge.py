"""KnowledgeMiddleware — injects relevant knowledge context before LLM calls.

Queries the KnowledgeStore with the last user message and adds
top-k results to the state's `context` field.

Also loads prior session summaries from disk and injects them as a
<prior_sessions> block at the start of each session's context.
"""

import json
import logging
import os

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from langgraph.prebuilt.chat_agent_executor import AgentState


log = logging.getLogger(__name__)


class KnowledgeMiddleware(AgentMiddleware):
    """Inject knowledge store context before each LLM call.

    Also loads prior session summaries from /sandbox/memory/ and injects
    them as a <prior_sessions> block so the agent has continuity across
    sessions without requiring an active knowledge store.
    """

    def __init__(self, knowledge_store, top_k: int = 5):
        super().__init__()
        self._store = knowledge_store
        self._top_k = top_k
        # Lazily loaded on first before_model call; None = not yet loaded
        self._prior_sessions_cache: str | None = None

    # ---------------------------------------------------------------------------
    # Session memory loading
    # ---------------------------------------------------------------------------

    def load_memory(
        self,
        memory_path: str = "/sandbox/memory/",
        max_sessions: int = 10,
        max_tokens: int = 2000,
    ) -> str:
        """Load prior session summaries and format as a <prior_sessions> block.

        Reads up to *max_sessions* most recent JSON files from *memory_path*,
        enforces *max_tokens* budget by dropping oldest sessions first, and
        returns a formatted XML block ready for injection into the system
        prompt context.

        Returns an empty string when the directory is missing (first run) or
        when no readable sessions exist.  Never raises — all errors are logged
        and treated as empty memory.

        Token counting uses the character-based approximation (chars // 4)
        because tiktoken is not guaranteed to be installed.
        """
        # --- Guard: directory must exist ---
        if not os.path.isdir(memory_path):
            log.info(
                "[knowledge] memory directory not found: %s — starting with empty prior_sessions",
                memory_path,
            )
            return ""

        # --- List JSON session files, sorted newest-first by mtime ---
        try:
            entries: list[tuple[float, str]] = []
            for fname in os.listdir(memory_path):
                if not fname.endswith(".json"):
                    continue
                fpath = os.path.join(memory_path, fname)
                try:
                    entries.append((os.path.getmtime(fpath), fpath))
                except OSError:
                    continue
            entries.sort(reverse=True)  # newest first
        except OSError as exc:
            log.warning(
                "[knowledge] cannot list memory directory %s: %s — treating as empty",
                memory_path,
                exc,
            )
            return ""

        if not entries:
            return "<prior_sessions/>"

        # --- Parse session files (skip malformed) ---
        summaries: list[dict] = []
        for _, fpath in entries[:max_sessions]:
            try:
                with open(fpath, encoding="utf-8") as fh:
                    data = json.load(fh)
                summaries.append(data)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                log.debug(
                    "[knowledge] skipping malformed session file %s: %s", fpath, exc
                )
                continue

        if not summaries:
            return "<prior_sessions/>"

        # --- Format each summary as XML ---
        def _format_summary(s: dict) -> str:
            ts = s.get("timestamp", "unknown")
            sid = s.get("session_id", "unknown")
            lines = [f'<session id="{sid}" timestamp="{ts}">']

            msgs: list[dict] = s.get("messages", [])
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
            return "\n".join(lines)

        # Token approximation — chars // 4 (no tiktoken dependency)
        def _count_tokens(text: str) -> int:
            return max(1, len(text) // 4)

        # --- Enforce token budget: drop oldest sessions (end of list) ---
        formatted = [_format_summary(s) for s in summaries]
        while formatted:
            joined = "\n".join(formatted)
            if _count_tokens(joined) <= max_tokens:
                break
            formatted.pop()  # remove oldest (newest-first ordering)

        if not formatted:
            return "<prior_sessions/>"

        return "<prior_sessions>\n" + "\n".join(formatted) + "\n</prior_sessions>"

    # ---------------------------------------------------------------------------
    # Middleware hooks
    # ---------------------------------------------------------------------------

    def before_model(self, state, runtime) -> dict | None:
        """Query knowledge store with last user message, inject context.

        Also prepends prior session summaries on the first call so the
        agent has cross-session continuity from the very first LLM turn.
        """
        parts: list[str] = []

        # Load prior sessions once per middleware instance (lazy cache)
        if self._prior_sessions_cache is None:
            self._prior_sessions_cache = self.load_memory()
        if self._prior_sessions_cache:
            parts.append(self._prior_sessions_cache)

        messages = state.get("messages", [])
        if messages:
            # Find the last human message
            last_human: str | None = None
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    last_human = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    break

            if last_human:
                results = self._store.search(last_human, k=self._top_k)
                if results:
                    context_parts = ["[Relevant knowledge from previous sessions:]"]
                    for r in results:
                        context_parts.append(f"- [{r['table']}] {r['preview']}")
                    parts.append("\n".join(context_parts))

        if not parts:
            return None

        return {"context": "\n\n".join(parts)}

    async def abefore_model(self, state, runtime) -> dict | None:
        """Async version — same logic."""
        return self.before_model(state, runtime)
