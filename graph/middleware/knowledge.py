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

**The projection is turn-scoped, not instance-scoped.** One compiled graph (and so
one instance of this middleware) serves every concurrent turn — A2A callers, the
console, background jobs, goal loops, the scheduler. The composed projection
therefore rides the RUN's own state, in a private ``UntrackedValue`` channel
(:data:`TURN_PROJECTION_KEY`): ``before_agent`` writes it, ``wrap_model_call``
reads it back off ``request.state``. Each run has its own channel values, so
overlapping turns each deliver their own composed context; and an untracked
channel is never written to a checkpoint (nor to pending writes), which keeps
the ADR 0108 D2 contract — the projection never enters the checkpointer.

A HITL resume (``Command(resume=…)`` after an ``interrupt()``) continues the turn
from the interrupted node: ``before_agent`` does not run again, and the untracked
channel starts empty after the checkpoint load. ``before_model`` covers that case —
when the channel is ABSENT (``before_agent`` always writes it, even as an empty
marker, on any run that enters at the top), the run is a resume of a turn already
in progress, so the projection is composed once, lazily, and written back for the
rest of the run's model calls. Its retrieval query is the turn's newest OPERATOR
input (:func:`is_turn_input`), skipping the guard notes and summaries the runtime may
have written above it, and it writes its own injection-log row (ADR 0069 D6).
"""

import logging
from typing import TYPE_CHECKING, Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import PrivateStateAttr
from langchain_core.messages import HumanMessage
from langgraph.channels.untracked_value import UntrackedValue

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

# The run-scoped channel the composed projection rides from ``before_agent`` to
# every model call of the same turn: ``{"text": str, "sections": list | None}``.
TURN_PROJECTION_KEY = "protoagent_turn_projection"


class KnowledgeState(AgentState):
    """The private, never-checkpointed channel carrying this turn's projection.

    ``UntrackedValue``: lives for one run only (a resume or the next turn starts
    without it) and is skipped by both checkpoint and pending-write persistence.
    ``PrivateStateAttr``: omitted from the graph's input and output schemas, so
    a caller cannot supply a projection, and it is absent from invoke results,
    state snapshots and checkpoints. (It does appear in LangGraph's own
    ``stream_mode="values"``/``"updates"`` frames and ``astream_events`` chain
    payloads — none of which the server forwards or records.)

    Value shape: ``{"text": str, "sections": list | None}``; ``{}`` is the marker
    ``before_agent`` writes when it composed nothing, so the channel's PRESENCE
    means "this run entered at the top" and its absence means "resumed run".
    """

    protoagent_turn_projection: NotRequired[Annotated[dict | None, UntrackedValue, PrivateStateAttr]]


# ``additional_kwargs["lc_source"]`` values that mark a conversation summary written
# over the thread: langchain's SummarizationMiddleware (``graph/middleware/compaction.py``)
# and the operator-triggered compaction op (``graph/compaction_op.py``).
_SUMMARY_SOURCES = frozenset({"summarization", "compaction"})


def is_turn_input(message: Any) -> bool:
    """Is ``message`` operator input — a message that may stand for the turn's ask?

    A ``HumanMessage`` that the runtime did NOT write onto the thread itself. Excluded:
    context frames (``graph.context_frame``), guard notes (round governor, stall guard,
    completion guard — recognised by tag, ``guard_notes``) and conversation summaries
    (``lc_source`` in :data:`_SUMMARY_SOURCES`). A folded steer (``SteeringMiddleware``)
    COUNTS: it is the operator's own text, typed mid-turn to redirect it, so it is the
    freshest statement of what the turn is for — the same line the round governor draws
    for its turn boundary.
    """
    if not isinstance(message, HumanMessage):
        return False
    from graph.context_frame import is_context_frame
    from graph.middleware.guard_notes import is_guard_note

    if is_context_frame(message) or is_guard_note(message):
        return False
    return (getattr(message, "additional_kwargs", None) or {}).get("lc_source") not in _SUMMARY_SOURCES


def turn_query(messages: Any) -> str:
    """The retrieval query for a turn: the newest operator input's text (a folded steer
    without its model-facing frame), or ``""`` when the thread has none."""
    from graph.middleware.steering import strip_interjection

    for msg in reversed(messages or []):
        if is_turn_input(msg):
            text = msg.content if isinstance(msg.content, str) else str(msg.content)
            return strip_interjection(text)
    return ""


def turn_projection(state_or_update: Any) -> tuple[str, list[dict] | None]:
    """The ``(text, sections)`` a ``before_agent`` update or a run's state carries —
    ``("", None)`` when nothing was composed for this turn."""
    try:
        value = (state_or_update or {}).get(TURN_PROJECTION_KEY)
    except AttributeError:  # not a mapping
        value = None
    if not isinstance(value, dict):
        return "", None
    return str(value.get("text") or ""), value.get("sections")


_WORKING_STATE_LABEL = "Working state"


def _tools_ran_this_turn(messages: Any) -> bool:
    """Has a tool result landed since the turn's newest operator input?

    The turn's first model call is served by the compose that just ran, so re-reading
    the working state there would only repeat it. Once a tool has run, a store the block
    reads (a task, the goal plan, a schedule) may have changed under it."""
    from langchain_core.messages import ToolMessage

    for msg in reversed(messages or []):
        if isinstance(msg, ToolMessage):
            return True
        if is_turn_input(msg):
            return False
    return False


def refresh_working_state(text: str, sections: list[dict] | None, state: Any) -> tuple[str, list[dict] | None]:
    """``(text, sections)`` with the ``<working_state>`` part re-read from the stores.

    The turn's projection is composed once in ``before_agent`` (ADR 0108 D2), but the
    working state is the one part the agent itself changes mid-turn: ``update_task``
    reported success, and the next model call was still shown the task's prior status
    from the turn-entry snapshot — so the agent re-did or second-guessed the update.
    Every other part (memory, RAG hits, the skill index) stays the turn's snapshot.

    Working state is the LAST part the composer assembles and is never shed by the
    budget (ADR 0108 D6), so it is exactly the trailing ``chars`` of ``text`` named by
    the final ``"Working state"`` section; it is swapped in place, so its position in
    the frame never moves. A block that appears (or empties) mid-turn is appended (or
    removed) with the composer's ``"\n\n"`` separator. Best-effort: any failure keeps
    the composed text.
    """
    try:
        from graph import projection

        fresh = projection.working_state_block(state if isinstance(state, dict) else {})
    except Exception as exc:  # noqa: BLE001 - a refresh must never break a model call
        log.debug("[knowledge] working-state refresh failed: %s", exc)
        return text, sections
    secs = [dict(s) for s in (sections or []) if isinstance(s, dict)]
    old_len = 0
    if secs and secs[-1].get("label") == _WORKING_STATE_LABEL:
        old_len = int(secs[-1].get("chars") or 0)
        secs.pop()
    elif sections is None:
        # No section annotations to locate the part by — leave the snapshot alone rather
        # than guess at the text's structure.
        return text, sections
    if old_len and text[len(text) - old_len :] == fresh:
        return text, sections
    base = text[: len(text) - old_len] if old_len else text
    if old_len and base.endswith("\n\n"):
        base = base[:-2]  # the separator that joined the working state on
    if fresh:
        text = f"{base}\n\n{fresh}" if base else fresh
        secs.append({"label": _WORKING_STATE_LABEL, "chars": len(fresh)})
    else:
        text = base
    return text, secs


class KnowledgeMiddleware(AgentMiddleware):
    """Inject knowledge store context before each LLM call.

    Also loads prior session summaries from the session-memory dir (see
    ``graph.middleware.memory.MEMORY_PATH``) and injects them as a
    <prior_sessions> block so the agent has continuity across sessions
    without requiring an active knowledge store.
    """

    state_schema = KnowledgeState

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
        config=None,
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
        # The config ``options`` was read from, kept so the window-derived knobs
        # (budget + skill-index cap) can be re-read against THIS turn's model —
        # the console's per-chat override (ADR 0108 D6). None = no per-model
        # resolution; the construction-time options stand for every turn.
        # Memo is per instance, not per process: a config hot-reload builds a new
        # middleware, so the derived options can never outlive the config they
        # came from. Keyed by the resolved model name ("" = configured default).
        self._config = config
        self._options_by_model: dict[str, ProjectionOptions] = {}
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
        self._prior_sessions_max: int = 10  # entries the digest SHOWS (the pool holds one spare)
        # ADR 0108 D2: the per-turn projection is composed in before_agent and
        # delivered ephemerally via wrap_model_call (request.override) so it
        # never enters the checkpointer. It is carried in the RUN's state
        # (TURN_PROJECTION_KEY), never on this instance: the instance is shared
        # by every concurrent turn.

    def _options(self, state=None) -> ProjectionOptions:
        """This middleware's delivery knobs in the shared composer's shape — the
        ``options`` it was built with (agent.py's ``from_config`` wiring, budget
        included), else the individual kwargs (unbounded delivery).

        ``state`` supplies the turn's model (the console's per-chat override):
        the budget and skill-index cap are sized off the model window, so a tab
        switched to a smaller model must not keep the default model's allowance.
        Without a config to re-read, or with no override on this turn, the
        construction-time options stand — today's numbers, unchanged.
        """
        if self._config is not None and state is not None:
            model = ""
            try:
                model = str((state or {}).get("model") or "").strip()
            except AttributeError:  # not a mapping — fall through to the built options
                model = ""
            if model:
                cached = self._options_by_model.get(model)
                if cached is None:
                    cached = ProjectionOptions.from_config(self._config, model_name=model)
                    self._options_by_model[model] = cached
                return cached
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
        max_sessions: int | None = None,
        max_tokens: int | None = None,
        *,
        exclude_session_id: str | None = None,
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

        ``max_sessions``/``max_tokens`` default to the configured ceilings
        (`memory.max_sessions` / `memory.max_tokens`, #3308) — explicit values
        still win, which is what the caps-changed reload in ``_cached_digest``
        relies on.

        ``exclude_session_id`` keeps the caller's own summary out of the RENDERED
        block (ADR 0108 D9). It DEFAULTS to the ambient tracing session — the same
        chain the summary writer keys on — which makes this public seam safe
        WHEREVER a session context is bound. It is a default, not a guarantee:
        with no tracing context (a background thread, an operator route, inside a
        tool body) it resolves to "" and the digest is unfiltered, so a caller who
        knows its session id should pass it. The composer always does.
        Concretely:
        a fork or plugin calling it mid-turn gets the guarantee the composer has,
        instead of the leak #3252 fixed. Pass ``""`` for a deliberately neutral
        digest; that is what the shared cache primer does, since a block cached
        with one session's exclusion baked in would hide that session from every
        other thread. (The stashed POOL is unfiltered for the same reason.)
        """
        from graph.middleware.memory import finish_digest, load_digest_pool
        from graph.middleware.memory import memory_path as _memory_path

        opts = self._options()
        max_sessions = opts.prior_sessions_max if max_sessions is None else max_sessions
        max_tokens = opts.prior_sessions_max_tokens if max_tokens is None else max_tokens
        if exclude_session_id is None:
            try:
                from observability import tracing

                exclude_session_id = tracing.current_session_id() or ""
            except Exception:  # noqa: BLE001 — no tracing context → no exclusion
                exclude_session_id = ""
        # One MORE than the digest shows (ADR 0108 D9): the cache is shared across
        # sessions, so the active-session exclusion can only run per call — the
        # spare entry is what refills the digest when the caller's own summary is
        # dropped from the pool. Trimmed back to max_sessions in _cached_digest,
        # AFTER that filter, so the refill happens on the path production uses.
        pool, exists = load_digest_pool(memory_path or _memory_path(), max_sessions + 1)
        # Stash the PRE-TRIM, UNFILTERED pool: _cached_digest re-filters and
        # re-renders per call while the disk read stays TTL-cached.
        self._prior_sessions_pool = list(pool)
        self._prior_sessions_dir_exists = exists
        self._prior_sessions_max = max_sessions
        shown = [e for e in pool if e.session_id != exclude_session_id] if exclude_session_id else pool
        res = finish_digest(shown[:max_sessions], max_tokens, dir_exists=exists)
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

        opts = self._options()
        policy = opts.prior_sessions_policy
        if policy == "off":  # defensive — the composer gates before calling
            return "", []
        if policy == "relevant":
            from graph.middleware.memory import load_digest

            return load_digest(
                "relevant",
                query=query,
                exclude_session_id=exclude_session_id,
                max_sessions=opts.prior_sessions_max,
                max_tokens=opts.prior_sessions_max_tokens,
            )
        now = time.monotonic()
        # A caps change (settings save) also invalidates: the pool is READ at
        # max_sessions + 1, so a raised ceiling can't be served by trimming what
        # is already in hand — it needs the wider disk read.
        if (
            (self._prior_sessions_cache is None and self._prior_sessions_pool is None)
            or (now - self._prior_sessions_loaded_at) > _PRIOR_SESSIONS_TTL_S
            or (self._prior_sessions_pool is not None and self._prior_sessions_max != opts.prior_sessions_max)
        ):
            self._prior_sessions_cache = self.load_memory(
                max_sessions=opts.prior_sessions_max,
                max_tokens=opts.prior_sessions_max_tokens,
                # Neutral on purpose: this block is SHARED across sessions.
                exclude_session_id="",
            )
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
            pool[: opts.prior_sessions_max],
            opts.prior_sessions_max_tokens,
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
        """Compose the turn's dynamic context ONCE and hand it to this run's
        model calls for ephemeral delivery via ``wrap_model_call`` (ADR 0108 D2,
        #3188).

        The projection is composed here (once per turn entry, not per model
        call) so it is stable within the tool loop. It is NOT a ``messages``
        update: it goes to the run-scoped :data:`TURN_PROJECTION_KEY` channel
        (untracked — never checkpointed), and ``wrap_model_call`` delivers it via
        ``request.override(messages=…)``.

        Guarded on the newest message being FRESH operator input
        (:func:`is_turn_input` — not a context frame, guard note or summary): a run
        that enters at the top without new input (a kicker retry) must not
        recompose, and delivers no projection. Either way the channel is WRITTEN (``{}``
        when nothing was composed): its presence tells ``before_model`` this run
        entered here. A HITL resume (``Command(resume=…)``) never runs this hook
        at all — ``before_model`` composes for it (see there).
        """
        messages = state.get("messages") or []
        if not messages or not is_turn_input(messages[-1]):
            return {TURN_PROJECTION_KEY: {}}  # re-entry without fresh input — no recompose
        return {TURN_PROJECTION_KEY: self._projection_value(state, runtime, record=True)}

    def _projection_value(self, state, runtime, *, record: bool) -> dict:
        composed = self.compose_context(state, runtime, record=record)
        ctx = (composed or {}).get("context") or ""
        if not ctx:
            return {}
        return {"text": ctx, "sections": (composed or {}).get("context_sections")}

    def before_model(self, state, runtime) -> dict | None:
        """Recompose for a RESUMED run — once, lazily.

        ``Command(resume=…)`` (ask_human, fs approvals, request_user_input, and the
        goal/scheduler auto-resume) continues the turn at the interrupted node, so
        ``before_agent`` does not run and the untracked channel starts empty. An
        absent channel therefore means "resume of a turn in progress" (a run that
        entered at the top always wrote it): compose from this thread's state and
        write the channel, so every later model call of the run delivers it. A
        present channel (the common case) is a no-op.

        The query is the newest OPERATOR input (:func:`turn_query`), never merely the
        newest ``HumanMessage``: by the time a turn is resumed, a guard note or a
        conversation summary may sit above the turn's own input.

        ``record=True``: this compose is what the resumed run's model calls actually
        receive, so it writes its own injection-log row (ADR 0069 D6) — the log holds
        one row per compose, so a turn resumed N times has N + 1 rows.
        """
        if TURN_PROJECTION_KEY in (state or {}):
            return None
        if not any(is_turn_input(m) for m in state.get("messages") or []):
            return {TURN_PROJECTION_KEY: {}}
        return {TURN_PROJECTION_KEY: self._projection_value(state, runtime, record=True)}

    async def abefore_model(self, state, runtime) -> dict | None:
        """Async ``before_model`` — the (rare) resume compose runs off the loop."""
        if TURN_PROJECTION_KEY in (state or {}):
            return None
        import asyncio

        return await asyncio.to_thread(self.before_model, state, runtime)

    def compose_context(self, state, runtime=None, *, record: bool = True) -> dict | None:
        """The dynamic-context composer behind ``before_agent`` — the shared
        :func:`graph.projection.compose_projected_context` (ADR 0108 D8) fed
        this graph turn's inputs: the newest operator input (:func:`turn_query`) as
        the retrieval query, ``state["incognito"]`` (ADR 0069 D3b), the TTL-cached digest,
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
        projected = compose_projected_context(
            turn_query(state.get("messages")),
            self._store,
            self._skills_index,
            state,
            incognito=bool(state.get("incognito")),
            record=record,
            options=self._options(state),
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
        composed projection is RETURNED (a run-scoped state update), never
        written to the instance; the only instance state mutated is the
        prior-sessions cache (a neutral, cross-session pool), which is benign
        across threads; the store opens a sqlite connection per call.
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

        The projection is read off THIS request's run state — never off the
        shared instance — so concurrent turns each see only their own.

        Its ``<working_state>`` part is the one exception to "composed once": once a
        tool has run this turn it is re-read for every model call
        (:func:`refresh_working_state`), so a task the agent just closed is not shown
        to it as still open on the very next call.

        Stashes the projected text for PromptCaptureMiddleware (#3191).
        """
        text, sections = turn_projection(getattr(request, "state", None))
        msgs = getattr(request, "messages", None) or []
        if text and _tools_ran_this_turn(msgs):
            text, sections = refresh_working_state(text, sections, getattr(request, "state", None))
        return self._deliver(request, msgs, text, sections)

    @staticmethod
    def _deliver(request, msgs, text, sections):
        from graph.context_frame import context_frame_message, is_context_frame, stash_projected_context

        cleaned = [m for m in msgs if not is_context_frame(m)]
        if text:
            cleaned.append(context_frame_message(text))
            stash_projected_context(text, sections)
        if len(cleaned) != len(msgs) or text:
            return request.override(messages=cleaned)
        return request

    def wrap_model_call(self, request, handler):
        return handler(self._project_messages(request))

    async def awrap_model_call(self, request, handler):
        # The working-state refresh reads sqlite stores (tasks, goals, schedules) — keep
        # it off the event loop, the same posture as the turn compose (abefore_agent).
        text, sections = turn_projection(getattr(request, "state", None))
        msgs = getattr(request, "messages", None) or []
        if text and _tools_ran_this_turn(msgs):
            import asyncio

            text, sections = await asyncio.to_thread(
                refresh_working_state, text, sections, getattr(request, "state", None)
            )
        return await handler(self._deliver(request, msgs, text, sections))
