"""KnowledgeMiddleware — injects relevant knowledge context before LLM calls.

Queries the KnowledgeStore with the last user message and adds
top-k results to the state's `context` field.

Also loads prior session summaries from disk and injects them as a
<prior_sessions> block at the start of each session's context.

Retrieves relevant learned skills from the SkillsIndex and injects
them as a <learned_skills> block alongside <prior_sessions>.
"""

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage


if TYPE_CHECKING:
    from graph.skills.index import SkillRecord, SkillsIndex


log = logging.getLogger(__name__)

# Token budget for the <learned_skills> block (chars // 4 approximation)
_SKILLS_MAX_TOKENS = 2000
_SKILLS_CONTEXT_CHARS = 2000  # chars of recent context to include in query

# How long the <prior_sessions> block is cached before a disk reload. Bounds
# both staleness (sessions persisted after boot become visible within the TTL,
# instead of a frozen first-request snapshot for the process lifetime) and
# per-turn disk I/O.
_PRIOR_SESSIONS_TTL_S = 60.0


def _in_goal_turn() -> bool:
    """Whether the current turn is a goal-driven invocation.

    Lazy import keeps the middleware decoupled from the goals package and
    fail-safe (treat as a normal turn if the marker module is unavailable).
    """
    try:
        from graph.goals.goal_turn import in_goal_turn

        return in_goal_turn()
    except Exception:
        return False


class KnowledgeMiddleware(AgentMiddleware):
    """Inject knowledge store context before each LLM call.

    Also loads prior session summaries from /sandbox/memory/ and injects
    them as a <prior_sessions> block so the agent has continuity across
    sessions without requiring an active knowledge store.
    """

    def __init__(
        self,
        knowledge_store,
        top_k: int = 5,
        skills_index: "SkillsIndex | None" = None,
        skills_top_k: int = 5,
    ):
        super().__init__()
        self._store = knowledge_store
        self._top_k = top_k
        self._skills_index = skills_index
        self._skills_top_k = skills_top_k
        # Lazily loaded on first before_model call; None = not yet loaded.
        # Refreshed after _PRIOR_SESSIONS_TTL_S so sessions persisted after boot
        # become visible (the cache is otherwise frozen for the process life).
        self._prior_sessions_cache: str | None = None
        self._prior_sessions_loaded_at: float = 0.0

    # ---------------------------------------------------------------------------
    # Session memory loading
    # ---------------------------------------------------------------------------

    def load_memory(
        self,
        memory_path: str = "/sandbox/memory/",
        max_sessions: int = 10,
        max_tokens: int = 2000,
    ) -> str:
        """Format the most-recent persisted sessions as a ``<prior_sessions>``
        block for injection.

        Delegates to the shared :func:`graph.middleware.memory.load_prior_sessions`
        (ADR 0021) — one source of truth, with read-time reasoning stripping —
        so this and ``MemoryMiddleware`` can't drift. Never raises.
        """
        from graph.middleware.memory import load_prior_sessions

        return load_prior_sessions(memory_path, max_sessions, max_tokens)

    # ---------------------------------------------------------------------------
    # Skill retrieval
    # ---------------------------------------------------------------------------

    def load_skills(self, query: str, k: int | None = None) -> list["SkillRecord"]:
        """Retrieve top-k relevant skills from the SkillsIndex.

        Searches the FTS5 index using *query* and returns ranked results.
        Returns an empty list when no index is configured or there are no
        matches — callers must handle the empty case gracefully.

        Args:
            query: Combined user message + recent context (up to 2K chars).
            k:     Max results; defaults to self._skills_top_k.
        """
        if self._skills_index is None:
            return []
        if not query or not query.strip():
            return []
        k = k if k is not None else self._skills_top_k
        try:
            return self._skills_index.load_skills(query, k=k)
        except Exception as exc:  # pragma: no cover
            log.warning("[knowledge] skills retrieval error: %s", exc)
            return []

    def _build_skills_query(self, messages: list) -> str:
        """Build a query string from the last human message + recent context.

        Constructs query = last human message + last _SKILLS_CONTEXT_CHARS
        chars of recent AI/human message content, capped at 2K chars total.
        """
        last_human = ""
        context_parts: list[str] = []

        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                if not last_human:
                    last_human = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                else:
                    content = (
                        msg.content
                        if isinstance(msg.content, str)
                        else str(msg.content)
                    )
                    context_parts.append(content)
            elif isinstance(msg, AIMessage):
                content = (
                    msg.content
                    if isinstance(msg.content, str)
                    else str(msg.content)
                )
                context_parts.append(content)

        recent_context = " ".join(reversed(context_parts))
        if recent_context:
            recent_context = recent_context[-_SKILLS_CONTEXT_CHARS:]

        query = (last_human + " " + recent_context).strip()
        return query[:_SKILLS_CONTEXT_CHARS]

    def _format_learned_skills(self, skills: list["SkillRecord"]) -> str:
        """Format retrieved skills as a <learned_skills> XML block.

        Enforces a token budget of _SKILLS_MAX_TOKENS (chars // 4 approximation).
        Iteratively removes lowest-relevance skills (highest score, least negative
        BM25 value) then truncates descriptions if still over budget.

        Returns an empty string when *skills* is empty.
        """
        if not skills:
            return ""

        def _count_tokens(text: str) -> int:
            return max(1, len(text) // 4)

        def _format_skill(s: "SkillRecord") -> str:
            # Truncate prompt_template to 500 chars to keep block concise
            pt = s.prompt_template[:500] if s.prompt_template else ""
            # Surface the skill's declared tools so the model knows which of its
            # (already-bound) tools this skill relies on — a relevance hint, not
            # a gate. See ADR 0005 (tool pollution / progressive disclosure).
            tools = getattr(s, "tools_used", ()) or ()
            tools_line = (
                f"    <relevant_tools>{', '.join(tools)}</relevant_tools>\n" if tools else ""
            )
            return (
                f'  <skill name="{s.name}">\n'
                f"    <description>{s.description}</description>\n"
                f"{tools_line}"
                f"    <prompt_template>{pt}</prompt_template>\n"
                f"  </skill>"
            )

        # Sort by score ascending (most relevant first, BM25 scores are negative)
        sorted_skills = sorted(skills, key=lambda s: s.score)

        formatted = [_format_skill(s) for s in sorted_skills]

        # Enforce token budget: remove lowest-relevance skills first (end of list)
        while formatted:
            block = "<learned_skills>\n" + "\n".join(formatted) + "\n</learned_skills>"
            if _count_tokens(block) <= _SKILLS_MAX_TOKENS:
                break
            formatted.pop()
            if formatted:
                log.debug("[knowledge] skills token budget exceeded — removed lowest-relevance skill")

        if not formatted:
            return ""

        # If still over budget after removing all but one, truncate descriptions
        block = "<learned_skills>\n" + "\n".join(formatted) + "\n</learned_skills>"
        if _count_tokens(block) > _SKILLS_MAX_TOKENS:
            # Hard truncate the block to fit
            max_chars = _SKILLS_MAX_TOKENS * 4
            block = block[:max_chars]
            log.warning("[knowledge] skills block hard-truncated to fit token budget")

        return block

    # ---------------------------------------------------------------------------
    # Middleware hooks
    # ---------------------------------------------------------------------------

    def before_model(self, state, runtime) -> dict | None:
        """Query knowledge store with last user message, inject context.

        Also prepends prior session summaries on the first call so the
        agent has cross-session continuity from the very first LLM turn.

        Retrieves relevant learned skills from the SkillsIndex (when
        configured) and injects them as a <learned_skills> block.
        """
        parts: list[str] = []

        # Load prior sessions with a TTL cache (lazy + periodic refresh).
        # Suppressed on goal-driven turns: unrelated cross-session history
        # biases the self-driving loop (see graph.goals.goal_turn).
        import time

        now = time.monotonic()
        if (
            self._prior_sessions_cache is None
            or (now - self._prior_sessions_loaded_at) > _PRIOR_SESSIONS_TTL_S
        ):
            self._prior_sessions_cache = self.load_memory()
            self._prior_sessions_loaded_at = now
        if self._prior_sessions_cache and not _in_goal_turn():
            parts.append(self._prior_sessions_cache)

        # Hot memory — always-on operator facts (domain="hot"). Loaded per turn
        # (not cached) so a freshly-added hot fact is seen immediately.
        if self._store is not None and hasattr(self._store, "get_hot_memory"):
            try:
                hot = self._store.get_hot_memory()
                if hot:
                    parts.append(f"[Always-on facts (hot memory):]\n{hot}")
            except Exception as exc:  # noqa: BLE001 - never break the loop on memory
                log.debug("[knowledge] hot memory load failed: %s", exc)

        messages = state.get("messages", [])

        # Inject learned skills from SkillsIndex when available
        if self._skills_index is not None and messages:
            skills_query = self._build_skills_query(messages)
            if skills_query:
                skills = self.load_skills(skills_query)
                skills_block = self._format_learned_skills(skills)
                if skills_block:
                    parts.append(skills_block)

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

            if last_human and self._store is not None:
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
        """Async version — same logic, off the event loop.

        ``before_model`` blocks: the store search embeds the query over HTTP
        (HybridKnowledgeStore + create_embed_fn), plus sqlite + disk reads for
        hot memory / prior sessions / skills. Running it inline here stalled
        the event loop before *every* LLM call, so it goes through
        ``asyncio.to_thread`` (same pattern as graph/checkpointer.py). The
        only state mutated is the prior-sessions cache (str + float
        assignment), which is benign across threads; the store opens a sqlite
        connection per call.
        """
        import asyncio

        return await asyncio.to_thread(self.before_model, state, runtime)
