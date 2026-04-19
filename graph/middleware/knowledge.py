"""KnowledgeMiddleware — injects relevant knowledge context before LLM calls.

Queries the KnowledgeStore with the last user message and adds
top-k results to the state's `context` field.

Also exposes ``load_skills(query, k)`` for retrieving top-k relevant
skill-v1 artifacts from the SQLite skill index.
"""

from __future__ import annotations

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from langgraph.prebuilt.chat_agent_executor import AgentState


class KnowledgeMiddleware(AgentMiddleware):
    """Inject knowledge store context before each LLM call."""

    def __init__(self, knowledge_store, top_k: int = 5, skill_index=None):
        super().__init__()
        self._store = knowledge_store
        self._top_k = top_k
        # Optional SkillIndex for load_skills() retrieval.
        self._skill_index = skill_index

    def before_model(self, state, runtime) -> dict | None:
        """Query knowledge store with last user message, inject context."""
        messages = state.get("messages", [])
        if not messages:
            return None

        # Find the last human message
        last_human = None
        for msg in reversed(messages):
            if isinstance(msg, HumanMessage):
                last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
                break

        if not last_human:
            return None

        # Search knowledge store
        results = self._store.search(last_human, k=self._top_k)
        if not results:
            return None

        # Format context
        context_parts = ["[Relevant knowledge from previous sessions:]"]
        for r in results:
            context_parts.append(f"- [{r['table']}] {r['preview']}")

        return {"context": "\n".join(context_parts)}

    async def abefore_model(self, state, runtime) -> dict | None:
        """Async version — same logic."""
        return self.before_model(state, runtime)

    # ------------------------------------------------------------------
    # Skill retrieval
    # ------------------------------------------------------------------

    def load_skills(self, query: str, k: int = 5) -> list[dict]:
        """Return up to *k* skill-v1 artifacts ranked by relevance to *query*.

        Searches the skill index (SQLite FTS5 or LIKE fallback) using the
        provided query string — typically the current user message combined
        with recent conversation context (last ~2 K chars).

        Parameters
        ----------
        query:
            Free-text search string.
        k:
            Maximum number of results to return (default 5).

        Returns
        -------
        list[dict]
            Each dict contains: ``id``, ``name``, ``description``,
            ``prompt_template``, ``tools_used``, ``created_at``,
            ``source_session_id``, ``score``.
            Returns an empty list when no skill index is configured or
            when the index contains no relevant results.
        """
        if self._skill_index is None:
            return []
        return self._skill_index.search(query, k=k)
