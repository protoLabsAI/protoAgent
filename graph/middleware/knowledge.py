"""KnowledgeMiddleware — injects relevant knowledge context before LLM calls.

Composes the per-turn dynamic context projection ONCE in ``before_agent`` and
delivers it ephemerally via ``wrap_model_call`` (ADR 0108 D2) — the projection
never enters the checkpointer.

The composition itself lives in :mod:`graph.projection`
(:func:`~graph.projection.compose_projected_context`, ADR 0108 D8): one
projection for every runtime, so an external brain (``runtime/context.py``)
is fed exactly what this middleware injects — the ``<injected_memory>``
envelope (prior-session digest, hot memory, trust-ranked RAG hits), the
always-on ``<available_skills>`` index (ADR 0060), and the agent's own
``<working_state>`` (ADR 0079). This class owns what is graph-specific: the
turn-entry guard, the digest's TTL cache, and the ephemeral delivery.
"""

import logging
from typing import TYPE_CHECKING

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from graph.projection import (
    ProjectionOptions,
    compose_projected_context,
    record_injection,
    skill_index_block,
    working_state_block,
)


if TYPE_CHECKING:
    from graph.skills.index import SkillsIndex


log = logging.getLogger(__name__)

# How long the <prior_sessions> block is cached before a disk reload. Bounds
# both staleness (sessions persisted after boot become visible within the TTL,
# instead of a frozen first-request snapshot for the process lifetime) and
# per-turn disk I/O.
_PRIOR_SESSIONS_TTL_S = 60.0


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
        skills_top_k: int = 24,
        skills_index_chars: int = 8192,
        inject_namespaces: list[str] | None = None,
        inject_min_trust: int = 1,
        options: ProjectionOptions | None = None,
    ):
        super().__init__()
        # ``options`` (ADR 0108 D6) is THE wiring graph/agent.py uses —
        # ``ProjectionOptions.from_config(config)`` — and carries the one knob the
        # individual kwargs can't (the projected-context budget). When given it
        # overrides the individual kwargs, which stay for tests and direct callers.
        if options is not None:
            top_k = options.top_k
            skills_top_k = options.skills_top_k
            skills_index_chars = options.skills_index_chars
            inject_namespaces = list(options.inject_namespaces)
            inject_min_trust = options.inject_min_trust
        self._options_override = options
        self._store = knowledge_store
        self._top_k = top_k
        # Trust floor for the auto-inject RAG hits (ADR 0069 D8,
        # `knowledge.inject_min_trust`). 1 (the default) excludes nothing —
        # low-trust hits are only DOWN-WEIGHTED (ranked below higher tiers);
        # 2 drops ingested/web/external content from auto-injection entirely;
        # 3 auto-injects operator-authored rows only. Tool-driven recall
        # (memory_recall) is never gated — excluded content stays reachable
        # on demand, with the tier visible in the tool output.
        self._inject_min_trust = max(1, int(inject_min_trust))
        self._skills_index = skills_index
        # #2867: every discoverable skill is ALWAYS listed. skills_top_k caps how
        # many carry their full DESCRIPTION (compat with the old count knob);
        # skills_index_chars is the char ceiling for the block (~2% of the model
        # window, 8KB when the window is unknown; <=0 = uncapped). Overflow rows
        # keep their name+slash — identities never drop.
        self._skills_top_k = skills_top_k
        self._skills_index_chars = int(skills_index_chars)
        # Namespace scope for the auto-inject RAG search (ADR 0069 D3a,
        # `knowledge.inject_namespaces`). Empty/None = unfiltered (today's
        # behavior — box-commons sharing keeps working); "" in the list matches
        # un-namespaced chunks. Tool-driven recall (memory_recall) is NOT
        # scoped by this — it only gates what enters the prompt unasked.
        self._inject_namespaces = list(inject_namespaces or [])
        # Lazily loaded on first before_model call; None = not yet loaded.
        # Refreshed after _PRIOR_SESSIONS_TTL_S so sessions persisted after boot
        # become visible (the cache is otherwise frozen for the process life).
        self._prior_sessions_cache: str | None = None
        self._prior_sessions_ids: list[str] = []
        self._prior_sessions_loaded_at: float = 0.0
        # ADR 0108 D9: the TTL cache holds the RAW newest-N entry POOL, not a
        # rendered block — this middleware is shared across sessions, and the
        # active-session exclusion is per call, so a rendered cache would bake
        # one session's exclusion into every other session's digest. ``None``
        # marks "never loaded" (a test that primes _prior_sessions_cache
        # directly keeps the legacy rendered-block path).
        self._prior_sessions_pool: list | None = None
        self._prior_sessions_dir_exists: bool = True
        self._prior_sessions_max_tokens: int = 2000
        self._prior_sessions_max: int = 10  # entries the digest SHOWS (the pool holds one spare)
        # ADR 0108 D2: the per-turn projection is composed in before_agent and
        # delivered ephemerally via wrap_model_call (request.override) so it
        # never enters the checkpointer.  Stable within the turn's tool loop.
        self._turn_projection: str | None = None
        self._turn_sections: list[dict] | None = None

    def _options(self) -> ProjectionOptions:
        """This middleware's delivery knobs in the shared composer's shape — the
        ``options`` it was built with (agent.py's ``from_config`` wiring, budget
        included), else the individual kwargs (unbounded delivery)."""
        if self._options_override is not None:
            return self._options_override
        return ProjectionOptions(
            top_k=self._top_k,
            inject_namespaces=tuple(self._inject_namespaces),
            inject_min_trust=self._inject_min_trust,
            skills_top_k=self._skills_top_k,
            skills_index_chars=self._skills_index_chars,
        )

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

        Delegates to the shared :func:`graph.middleware.memory.load_prior_sessions_digest`
        (ADR 0021) — one source of truth, with read-time reasoning stripping —
        so this and ``SessionSummaryMiddleware`` can't drift. ``memory_path`` defaults
        to the writer's resolved ``memory_path()`` (no duplicate path literal,
        same can't-drift reasoning). Also stashes the digest's session ids on
        ``self._prior_sessions_ids`` so the per-turn injection record (ADR 0069
        D6) can attribute what was injected. Never raises.
        """
        from graph.middleware.memory import finish_digest, load_digest_pool
        from graph.middleware.memory import memory_path as _memory_path

        # One MORE than the digest shows (ADR 0108 D9): the cache is shared across
        # sessions, so the active-session exclusion can only run per call — the
        # spare entry is what refills the digest when the caller's own summary is
        # dropped from the pool. Trimmed back to max_sessions in _cached_digest,
        # AFTER that filter, so the refill happens on the path production uses.
        pool, exists = load_digest_pool(memory_path or _memory_path(), max_sessions + 1)
        # Stash the PRE-TRIM pool: _cached_digest re-filters and re-renders per
        # call while the disk read stays TTL-cached.
        self._prior_sessions_pool = list(pool)
        self._prior_sessions_dir_exists = exists
        self._prior_sessions_max_tokens = max_tokens
        self._prior_sessions_max = max_sessions
        res = finish_digest(pool[:max_sessions], max_tokens, dir_exists=exists)
        self._prior_sessions_ids = [e.session_id for e in res.entries]
        return res.block

    def _cached_digest(self, *, query: str = "", exclude_session_id: str = ""):
        """The TTL-cached prior-sessions digest (lazy + periodic refresh) — what
        this middleware hands the shared composer instead of a fresh disk read.

        ADR 0108 D9: under ``context.prior_sessions: relevant`` the digest is
        query-dependent by definition, so the pool cache is bypassed and the
        canonical loader runs fresh; under ``newest`` the cached POOL is
        filtered for the calling session (its own summary is never a "prior"
        session) and token-trimmed per call. A test that primed
        ``_prior_sessions_cache`` with a rendered block keeps getting exactly
        that block (legacy ``(block, ids)`` shape — the composer sheds it as
        one unit)."""
        import time

        policy = self._options().prior_sessions_policy
        if policy == "off":  # defensive — the composer gates before calling
            return "", []
        if policy == "relevant":
            from graph.middleware.memory import load_digest

            return load_digest("relevant", query=query, exclude_session_id=exclude_session_id)
        now = time.monotonic()
        if (
            self._prior_sessions_cache is None and self._prior_sessions_pool is None
        ) or (now - self._prior_sessions_loaded_at) > _PRIOR_SESSIONS_TTL_S:
            self._prior_sessions_cache = self.load_memory()
            self._prior_sessions_loaded_at = now
        if self._prior_sessions_pool is None:
            # Legacy primed-block path (tests): serve the block verbatim.
            return self._prior_sessions_cache or "", list(self._prior_sessions_ids)
        from graph.middleware.memory import finish_digest

        pool = self._prior_sessions_pool
        if exclude_session_id:
            pool = [e for e in pool if e.session_id != exclude_session_id]
        # Trim AFTER the exclusion (the pool holds one spare) so dropping the
        # caller's own summary refills from disk instead of shortening the digest.
        res = finish_digest(
            pool[: self._prior_sessions_max],
            self._prior_sessions_max_tokens,
            dir_exists=self._prior_sessions_dir_exists,
        )
        self._prior_sessions_ids = [e.session_id for e in res.entries]
        return res

    # ---------------------------------------------------------------------------
    # Parts — thin delegates to graph.projection (ADR 0108 D8)
    # ---------------------------------------------------------------------------

    def _skill_index_block(self) -> str:
        """The always-on ``<available_skills>`` index (ADR 0060, #2867) with this
        middleware's caps — a test-facing call surface only. ``compose_context``
        does NOT route through it: to influence the projection, patch
        ``graph.projection._skill_index``."""
        return skill_index_block(self._skills_index, top_k=self._skills_top_k, chars=self._skills_index_chars)

    def _working_state_block(self, state) -> str:
        """The agent's own live commitments (ADR 0079) — a test-facing call surface
        only. ``compose_context`` does NOT route through it: to influence the
        projection, patch ``graph.projection.working_state_block``."""
        return working_state_block(state)

    def _record_injection(
        self,
        state,
        memory_parts: list[str],
        digest_ids: list[str],
        hot_ids: list[int],
        rag_ids: list[int],
    ) -> None:
        """Append this model call's injected-memory row to the per-instance
        injection log (ADR 0069 D6) — :func:`graph.projection.record_injection`.
        The ONE instance-level patch point the composer honors: ``compose_context``
        threads it in as ``record_fn``, so patching it here takes effect."""
        record_injection(state, memory_parts, digest_ids, hot_ids, rag_ids)

    # ---------------------------------------------------------------------------
    # Middleware hooks
    # ---------------------------------------------------------------------------

    def before_agent(self, state, runtime) -> dict | None:
        """Compose the turn's dynamic context ONCE and stash it for ephemeral
        delivery via ``wrap_model_call`` (ADR 0108 D2, #3188).

        The projection is composed here (once per turn entry, not per model
        call) so it is stable within the tool loop, but is NOT returned as a
        ``messages`` state update — ``wrap_model_call`` delivers it via
        ``request.override(messages=…)`` which never enters the checkpointer.

        Guarded on the newest message being a FRESH human input: a HITL resume
        (``Command(resume=…)``) or a kicker retry re-enters the graph without new
        input, and must not recompose.
        """
        from graph.context_frame import is_context_frame

        messages = state.get("messages") or []
        last = messages[-1] if messages else None
        if not isinstance(last, HumanMessage) or is_context_frame(last):
            self._turn_projection = None
            return None  # re-entry without fresh input — no recompose, no state churn
        composed = self.compose_context(state, runtime, record=True)
        ctx = (composed or {}).get("context") or ""
        if not ctx:
            self._turn_projection = None
            self._turn_sections = None
            return None
        self._turn_projection = ctx
        self._turn_sections = (composed or {}).get("context_sections")
        return None

    def compose_context(self, state, runtime=None, *, record: bool = True) -> dict | None:
        """The dynamic-context composer behind ``before_agent`` — the shared
        :func:`graph.projection.compose_projected_context` (ADR 0108 D8) fed
        this graph turn's inputs: the last human message as the retrieval
        query, ``state["incognito"]`` (ADR 0069 D3b), the TTL-cached digest,
        and this middleware's delivery knobs.

        ``record=False`` is the SPECULATIVE path (#2388 P3 next-call preview): it
        runs the full dynamic layer — digest, hot memory, RAG retrieval, skill
        index, working state — but skips the ADR 0069 D6 injection-log write, so
        previewing a prompt never fabricates a "this entered the turn" record.

        Returns the ``{"context", "context_sections"}`` pair (both keys always
        move together — nothing composed is ``""`` + ``[]``), plus a ``budget``
        summary (``{"chars", "used", "overflow"}``) when a projected-context
        budget is configured (ADR 0108 D6).
        """
        last_human: str | None = None
        for msg in reversed(state.get("messages") or []):
            if isinstance(msg, HumanMessage):
                last_human = msg.content if isinstance(msg.content, str) else str(msg.content)
                break
        projected = compose_projected_context(
            last_human or "",
            self._store,
            self._skills_index,
            state,
            incognito=bool(state.get("incognito")),
            record=record,
            options=self._options(),
            prior_sessions=self._cached_digest,
            record_fn=self._record_injection,
        )
        return projected.as_legacy_dict()

    async def abefore_agent(self, state, runtime) -> dict | None:
        """Async version — same logic, off the event loop.

        ``before_agent`` blocks: the store search embeds the query over HTTP
        (HybridKnowledgeStore + create_embed_fn), plus sqlite + disk reads for
        hot memory / prior sessions / skills. Running it inline stalled the
        event loop (originally before *every* LLM call; since #2776 it would be
        once per turn — still worth keeping off-loop), so it goes through
        ``asyncio.to_thread`` (same pattern as graph/checkpointer.py). The
        only state mutated is the prior-sessions cache (str + float
        assignment), which is benign across threads; the store opens a sqlite
        connection per call.
        """
        import asyncio

        return await asyncio.to_thread(self.before_agent, state, runtime)

    # ---------------------------------------------------------------------------
    # ADR 0108 D2 — ephemeral projection delivery
    # ---------------------------------------------------------------------------

    def _project_messages(self, request):
        """Strip stale checkpoint frames, append the fresh turn projection.

        Old checkpoints may contain context frames persisted under the v1
        delivery model.  They are retained in the checkpoint for audit but
        excluded from the model-visible surface here.  The fresh projection
        (composed once in ``before_agent``) is appended as the last message so
        the model sees current context without it entering the checkpointer.

        Stashes the projected text for PromptCaptureMiddleware (#3191).
        """
        from graph.context_frame import context_frame_message, is_context_frame, stash_projected_context

        msgs = getattr(request, "messages", None) or []
        cleaned = [m for m in msgs if not is_context_frame(m)]
        if self._turn_projection:
            cleaned.append(context_frame_message(self._turn_projection))
            stash_projected_context(self._turn_projection, self._turn_sections)
        if len(cleaned) != len(msgs) or self._turn_projection:
            return request.override(messages=cleaned)
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._project_messages(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._project_messages(request))
