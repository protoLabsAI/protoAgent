"""KnowledgeMiddleware — injects relevant knowledge context before LLM calls.

Queries the KnowledgeStore with the last user message and adds
top-k results to the state's `context` field.

Also loads prior session summaries from disk and injects them as a
<prior_sessions> block at the start of each session's context.

Injects the always-on skill index — the {name, description} of every
discoverable skill — as an <available_skills> block so the model knows what
it can do and loads a skill's full procedure on demand via ``load_skill``
(progressive disclosure, ADR 0060).
"""

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage


if TYPE_CHECKING:
    from graph.skills.index import SkillsIndex


log = logging.getLogger(__name__)

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

    Also loads prior session summaries from the session-memory dir (see
    ``graph.middleware.memory.MEMORY_PATH``) and injects them as a
    <prior_sessions> block so the agent has continuity across sessions
    without requiring an active knowledge store.
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
        # Max skills listed in the always-on <available_skills> index (the rest
        # are reachable via list_skills). The model loads any one's full body on
        # demand via load_skill — so this caps the per-turn "table of contents",
        # not what's usable.
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
        memory_path: str | None = None,
        max_sessions: int = 10,
        max_tokens: int = 2000,
    ) -> str:
        """Format the most-recent persisted sessions as a ``<prior_sessions>``
        block for injection.

        Delegates to the shared :func:`graph.middleware.memory.load_prior_sessions`
        (ADR 0021) — one source of truth, with read-time reasoning stripping —
        so this and ``SessionSummaryMiddleware`` can't drift. ``memory_path`` defaults
        to the writer's resolved ``MEMORY_PATH`` (no duplicate path literal,
        same can't-drift reasoning). Never raises.
        """
        from graph.middleware.memory import MEMORY_PATH, load_prior_sessions

        return load_prior_sessions(memory_path or MEMORY_PATH, max_sessions, max_tokens)

    # ---------------------------------------------------------------------------
    # Skill index (progressive disclosure — ADR 0060)
    # ---------------------------------------------------------------------------

    def _skill_index_block(self) -> str:
        """Build the always-on ``<available_skills>`` index.

        Lists the ``{name, description}`` (and ``/slash`` when user-facing) of up
        to ``self._skills_top_k`` discoverable skills, most-recently-used first —
        the cheap "table of contents" the model scans every turn. It calls
        ``load_skill(name)`` to pull a skill's full procedure only when it decides
        one is relevant, so nothing is matched against the conversation here (the
        old BM25 retrieval guessed relevance from the agent's own recent output
        and mis-loaded skills every turn — ADR 0060). Returns an empty string when
        no index is configured or it holds no discoverable skills; never raises.
        """
        if self._skills_index is None:
            return ""
        try:
            summaries = self._skills_index.skill_summaries(limit=self._skills_top_k)
            total = self._skills_index.discoverable_count()
        except Exception as exc:  # noqa: BLE001 — never break a turn on skill listing
            log.warning("[knowledge] skill index error: %s", exc)
            return ""
        if not summaries:
            return ""

        lines = [
            "<available_skills>",
            "  <!-- Learned procedures you can use. Each is a name + one-line summary; "
            "call load_skill(name) to read the full steps before following one. Don't guess its contents. -->",
        ]
        for s in summaries:
            slash = (s.get("slash") or "").strip()
            slash_attr = f' slash="/{slash}"' if slash else ""
            lines.append(f'  <skill name="{s["name"]}"{slash_attr}>{s.get("description", "")}</skill>')
        if total > len(summaries):
            lines.append(f"  <!-- +{total - len(summaries)} more — call list_skills to see them all. -->")
        lines.append("</available_skills>")
        return "\n".join(lines)

    # ---------------------------------------------------------------------------
    # Middleware hooks
    # ---------------------------------------------------------------------------

    def before_model(self, state, runtime) -> dict | None:
        """Query knowledge store with last user message, inject context.

        Also prepends prior session summaries on the first call so the
        agent has cross-session continuity from the very first LLM turn.

        Injects the always-on skill index (when a SkillsIndex is configured)
        as an <available_skills> block (ADR 0060).
        """
        parts: list[str] = []

        # Load prior sessions with a TTL cache (lazy + periodic refresh).
        # Suppressed on goal-driven turns: unrelated cross-session history
        # biases the self-driving loop (see graph.goals.goal_turn).
        import time

        now = time.monotonic()
        if self._prior_sessions_cache is None or (now - self._prior_sessions_loaded_at) > _PRIOR_SESSIONS_TTL_S:
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

        # Always-on skill index (progressive disclosure, ADR 0060): the
        # {name, description} of available skills, independent of the
        # conversation. The model pulls a full procedure on demand via load_skill.
        skill_block = self._skill_index_block()
        if skill_block:
            parts.append(skill_block)

        if messages:
            # Find the last human message
            last_human: str | None = None
            for msg in reversed(messages):
                if isinstance(msg, HumanMessage):
                    last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
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
