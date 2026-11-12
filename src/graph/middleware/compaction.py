"""SummarizationMiddleware that archives before it compacts, and counts.

langchain's ``SummarizationMiddleware`` summarizes old history near the context
limit. Its ``before_model`` / ``abefore_model`` hooks return a non-``None`` state
update **only** when they actually compact (otherwise ``None``). We subclass for
two additions:

1. **Archive-first (#2784, ADR 0101 D5).** Auto-compaction was the LOSSY path:
   the rewrite landed in the checkpoint, ``checkpoint_keep_per_thread`` pruning
   destroyed the pre-compaction rows, and the summarized-away history was simply
   gone — while the never-lossy manual ``/compact`` archived first. Now, when the
   parent decides to compact, the full transcript is archived to the history
   store (``chat-archive:<session_id>``, the same namespace ``/compact`` uses)
   BEFORE the update is returned — i.e. before anything is committed. Failure
   mode, operator-decided: attempt the archive; on failure, compact ANYWAY with
   a loud log and let the safety valve do its duty — this is the automatic path
   between the model and an overflow error, and purity loses to availability
   here. The manual path keeps its strict refusal.

   The archive row carries ``thread_id=<thread>`` and the dates of the messages it
   holds (#3493, see ``conversation_harvest.archive_payload``). An **incognito**
   turn is never archived (ADR 0069 D3b, the rule the retire harvest applies):
   it still compacts, because this is the overflow safety valve, and the
   summarized-away history is simply not kept.

2. **A Prometheus counter** on each real compaction (ADR 0006 — proves the
   lever fires, and how often).

3. **A failing compaction never fails the turn.** The summary is a separate model
   call made from ``before_model``; if it raises (a provider 429/5xx, a timeout, an
   auth rejection — e.g. the anthropic-oauth fake 429 when the identity block was
   missing), the parent's exception used to propagate out of the graph node and kill
   the user's turn. Now it is logged loudly and the turn proceeds UNcompacted — the
   history is untouched. Graph control flow (interrupts / ``GraphBubbleUp``) and
   cancellation still propagate. Because ``before_model`` runs before EVERY model
   step, a failure also pauses auto-compaction on that thread with exponential
   backoff (1 min doubling to 30 min, reset on success), warning once per window;
   the summary call retries only transient errors (408/409/429/5xx, timeouts), once,
   instead of langchain's retry-everything ``with_retry()``. A context overflow on a
   thread whose compaction is failing is re-raised naming that cause
   (:class:`CompactionFailedContextOverflow`; the provider's text is kept so the
   server's overflow recovery still matches it).

4. **The window is THIS turn's model's** (brandLaunch context burn, 10-2026). The
   parent sizes a ``fraction:`` trigger off ``self.model.profile`` — the SUMMARIZER's
   profile, built once from the config model. A chat tab on another model (the
   per-turn ``model`` override) or a cheap ``compaction.model`` was measured against
   the wrong window. A ``window_resolver(state)`` (``graph.model_window
   .context_window_for_turn``) now supplies the window per call; the summarizer's
   profile is only the fallback. The absolute ``compaction.max_tokens`` ceiling rides
   the trigger as an OR clause (graph/agent.py), so it applies on any window.

5. **Kept turns lose their thinking blocks.** Compaction is keep-tail; the kept turns'
   thinking blocks were produced with the full history in front of them, and Anthropic's
   preserved-thinking check rejects them after the rewrite (see
   :func:`_strip_retained_thinking`).

6. **The summary is the compaction engine's, not langchain's** (ADR 0127 D1/D2/D4).
   The parent summarized only the last 4,000 tokens of the span
   (``trim_tokens_to_summarize``, ``strategy="last"``) with its stock prompt — an
   operator's conditional approval behind one long tool result never reached the
   summarizer, and the previous compaction's summary at the head of the span was
   trimmed away too. ``_create_summary`` / ``_acreate_summary`` now hand the FULL
   to-be-summarized span to :mod:`graph.compaction_engine` (chunked fold, seeded from a
   previous summary, the continuation prompt with required sections, then the verbatim
   ``<operator_messages>`` block), and
   ``_build_new_messages`` frames the result as the agent's own notes. A summarizer
   failure raises into the failure policy above (skip + back off), exactly as before.

Telemetry and archiving are both best-effort: neither ever affects the model call.
"""

from __future__ import annotations

import contextvars
import logging

from langchain.agents.middleware import SummarizationMiddleware

from graph import compaction_engine
from graph.conversation_harvest import _DEFAULT_HISTORY_STORE, resolve_history_store

log = logging.getLogger(__name__)


def _surface_op(state, result) -> None:
    """Trajectory surface_op for an auto-compaction (ADR 0102 S1) — counts from
    the update the parent computed: [RemoveMessage(ALL), summary, *preserved]."""
    try:
        from observability.trajectory import log_surface_op

        update = list((result or {}).get("messages") or [])
        preserved = max(0, len(update) - 2)
        before = len(list((state or {}).get("messages") or []))
        log_surface_op(
            str((state or {}).get("session_id") or ""),
            "compact",
            cause="auto",
            removed=max(0, before - preserved),
            kept=preserved,
        )
    except Exception:  # noqa: BLE001 — the trajectory never touches a model call
        pass


def _strip_retained_thinking(result):
    """Drop thinking blocks from the messages the parent kept verbatim after the summary.

    langchain's summarization is keep-tail: ``[RemoveMessage(ALL), summary, *preserved]``.
    The preserved turns' thinking blocks were produced with the full history in front of
    them, and Anthropic's history-editing check (preserved thinking — enforced for
    accounts created on/after 2026-08-31, Opus 5.5 / Sonnet 5.5 / Fable 5.1 / Haiku 5.5)
    rejects every later request that replays them: one compaction would brick the thread.
    Stripping them from the retained turns is the documented fix; text and tool calls stay.
    """
    try:
        from graph.message_blocks import without_thinking

        update = list((result or {}).get("messages") or [])
        if not update:
            return result
        return {**result, "messages": [update[0], *(without_thinking(m) for m in update[1:])]}
    except Exception:  # noqa: BLE001 — never let the cleanup cost the compaction
        log.debug("[compaction] thinking strip skipped", exc_info=True)
        return result


def _thread_id() -> str | None:
    """The checkpoint thread this turn runs on, for the archive's ``thread_id``. The
    hooks run inside a graph node, so the run's config carries it; None outside a run."""
    try:
        from langgraph.config import get_config

        tid = ((get_config() or {}).get("configurable") or {}).get("thread_id")
    except Exception:  # noqa: BLE001 — no run context (a unit test calling the hook)
        return None
    return str(tid) if tid else None


def _is_system_thread(state) -> bool:
    """Whether this turn runs on a background ``system:*`` thread (#4625).

    ADR 0003's reactive producers — the scheduler's wakes, the inbox — route their turns
    into the single durable Activity context ``system:activity`` (``events.ACTIVITY_CONTEXT``);
    any future ``system:*`` context is treated the same. These accumulate and resend the
    whole thread every wake, so they get a much lower compaction ceiling than a chat thread.

    Keyed off the state's ``session_id`` (the clean ``system:activity``) as the reliable
    signal, with the checkpointer ``thread_id`` as a fallback — that carries a transport
    prefix (``a2a:system:activity``, see ``server/maintenance_loops.py``), so match the
    ``system:`` segment either at the start or right after one ``<transport>:`` prefix."""
    session_id = str((state or {}).get("session_id") or "")
    if session_id.startswith("system:"):
        return True
    tid = _thread_id() or ""
    return tid.startswith("system:") or ":system:" in tid


def _count() -> None:
    try:
        from observability import metrics

        metrics.record_compaction()
    except Exception:  # noqa: BLE001 — telemetry must never break a model call
        pass


def _is_control_flow(exc: BaseException) -> bool:
    """Graph control-flow signals (interrupt, ParentCommand) must never be swallowed."""
    try:
        from langgraph.errors import GraphBubbleUp
    except Exception:  # noqa: BLE001 — older/absent langgraph: nothing to exempt
        return False
    return isinstance(exc, GraphBubbleUp)


# ── failure policy: transient-only retry + per-thread backoff ────────────────
#
# langchain wraps the summary model in ``model.with_retry()`` — 3 attempts with
# jittered backoff on ANY exception, 400/401/403 included — and ``before_model`` runs
# before EVERY model call in the tool loop. Once a failure stopped killing the turn,
# a persistent one would repeat (with sleeps, times the SDK's own ``max_retries``) on
# every step of every turn, hammering a provider that is already refusing us. So:
# retry only transient errors, once; after a failure, skip compaction on that thread
# for an exponentially growing window; warn once per backoff window, not per step.

_SUMMARY_ATTEMPTS = 2  # transient errors only; the provider SDK retries underneath too
_SUMMARY_RETRY_DELAY_S = 1.0
_BACKOFF_BASE_S = 60.0
_BACKOFF_MAX_S = 30 * 60.0
_BACKOFF_MAX_ENTRIES = 1024

# Hysteresis for the system-thread ceiling (#4625): the minimum amount of removable
# history, as a fraction (``ceiling // this``) of the ceiling, before a ``system:*``
# thread (re)compacts. The gap between the trigger and the post-compaction size — stops a
# thread whose kept tail already exceeds the low ceiling from re-compacting every step.
_SYSTEM_HYSTERESIS_DIVISOR = 4
# The most a ``system:*`` thread keeps verbatim after its ceiling fires, as a fraction
# (``ceiling // this``) of the ceiling (#5106). Keep-by-messages alone kept the newest
# ``compaction.keep_messages`` (20) whatever their size: fed one 2–5k-token tool result
# per step, that tail was itself over the 30k ceiling, so the thread was over it again
# the moment it compacted and re-fired every step or two — each time rewriting the
# cached prefix. Capping the tail at half the ceiling leaves a real gap below it.
_SYSTEM_KEEP_DIVISOR = 2


def _status_of(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _is_transient(exc: BaseException) -> bool:
    """Worth retrying: 408/409/429, 5xx, timeouts and dropped connections. A non-429
    4xx (bad request, auth, permission) fails identically on retry."""
    status = _status_of(exc)
    if status is not None:
        return status in (408, 409, 429) or status >= 500
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "connection" in name


class _TransientRetry:
    """Stand-in for ``model.with_retry()``: retries transient errors only."""

    def __init__(self, model, attempts: int | None = None, delay_s: float | None = None):
        self._model = model
        self._attempts = max(1, _SUMMARY_ATTEMPTS if attempts is None else attempts)
        self._delay_s = _SUMMARY_RETRY_DELAY_S if delay_s is None else delay_s

    def invoke(self, *args, **kwargs):
        import time

        for attempt in range(1, self._attempts + 1):
            try:
                return self._model.invoke(*args, **kwargs)
            except Exception as exc:
                if attempt >= self._attempts or not _is_transient(exc):
                    raise
                time.sleep(self._delay_s)

    async def ainvoke(self, *args, **kwargs):
        import asyncio

        for attempt in range(1, self._attempts + 1):
            try:
                return await self._model.ainvoke(*args, **kwargs)
            except Exception as exc:
                if attempt >= self._attempts or not _is_transient(exc):
                    raise
                await asyncio.sleep(self._delay_s)


def _summary_call_config() -> dict:
    """langchain's config for its summary calls — tags them internal so the turn stream
    never shows the summarizer's tokens as the agent's reply."""
    try:
        from langchain.agents.middleware.summarization import _summary_config

        return _summary_config()
    except Exception:  # noqa: BLE001 — private helper moved; keep the purpose marker
        return {"metadata": {"lc_source": "summarization"}}


def _backoff_key(state) -> str:
    return _thread_id() or str((state or {}).get("session_id") or "unknown")


try:
    from langchain_core.exceptions import ContextOverflowError as _OverflowBase
except ImportError:  # pragma: no cover — older langchain-core
    _OverflowBase = RuntimeError  # type: ignore[misc,assignment]


class CompactionFailedContextOverflow(_OverflowBase):
    """A context-window overflow on a thread whose auto-compaction is failing.

    Carries the provider's original text (so ``graph.llm.is_context_overflow_error``
    and the server's overflow recovery still match it) plus the compaction failure
    that let the history grow this far. The original error is ``__cause__``.
    """


# The window of the model THIS call runs on, set around each hook. A ContextVar, not an
# attribute: one middleware instance serves every concurrent session's turns.
_TURN_WINDOW: contextvars.ContextVar[int | None] = contextvars.ContextVar("compaction_turn_window", default=None)

# Whether THIS call runs on a background ``system:*`` thread (#4625). Same rationale as
# ``_TURN_WINDOW``: one instance serves every concurrent session, and ``_should_summarize``
# (called deep inside the parent's before_model) has no access to ``state`` — so the hook
# resolves it once from ``state`` and stashes it here for the override to read.
_TURN_SYSTEM: contextvars.ContextVar[bool] = contextvars.ContextVar("compaction_turn_system_thread", default=False)


# ``(session_id, archived)`` for THIS call, for the operator block's pointer line (ADR 0127
# D3). Same rationale again: ``_create_summary`` gets only the span, not ``state``.
_TURN_SESSION: contextvars.ContextVar[tuple[str, bool]] = contextvars.ContextVar(
    "compaction_turn_session", default=("", True)
)


def _turn_session(state) -> tuple[str, bool]:
    state = state if isinstance(state, dict) else {}
    return str(state.get("session_id") or ""), not state.get("incognito")


class CountingSummarizationMiddleware(SummarizationMiddleware):
    """``SummarizationMiddleware`` + archive-first (#2784) + a compaction counter."""

    def __init__(
        self,
        *args,
        knowledge_store=None,
        history_store=_DEFAULT_HISTORY_STORE,
        window_resolver=None,
        default_window: int | None = None,
        system_thread_max_tokens: int = 0,
        **kwargs,
    ):
        # Set BEFORE the parent's __init__: it validates a ``fraction`` trigger by asking
        # ``_get_profile_limits``, which reads these.
        self._window_resolver = window_resolver
        self._default_window = default_window if isinstance(default_window, int) and default_window > 0 else None
        # A separate, lower token ceiling applied ONLY on background ``system:*`` threads
        # (#4625), OR-ed with the configured trigger (see ``_should_summarize``). 0 = off.
        try:
            self._system_thread_max_tokens = max(0, int(system_thread_max_tokens or 0))
        except (TypeError, ValueError):
            self._system_thread_max_tokens = 0
        super().__init__(*args, **kwargs)
        self._knowledge_store = knowledge_store
        self._history_store = history_store
        # Replace the parent's retry-everything ``with_retry()`` wrapper.
        self._summary_model = _TransientRetry(self.model)
        self._backoff: dict[str, dict] = {}

    # ── per-turn window ──────────────────────────────────────────────────────

    def _get_profile_limits(self) -> int | None:  # type: ignore[override]
        """The window a ``fraction`` trigger is sized off: this turn's model's (set by
        the hooks), else the configured model's, else the summarizer's own profile."""
        win = _TURN_WINDOW.get()
        if win:
            return win
        if getattr(self, "_default_window", None):
            return self._default_window
        return super()._get_profile_limits()

    def _bind_turn_window(self, state):
        resolver = getattr(self, "_window_resolver", None)
        win = None
        if resolver is not None:
            try:
                win = resolver(state)
            except Exception:  # noqa: BLE001 — sizing never breaks a turn; fall back
                log.debug("[compaction] per-turn window resolution failed", exc_info=True)
        return _TURN_WINDOW.set(win if isinstance(win, int) and win > 0 else None)

    # ── the summary: the compaction engine over the whole span (ADR 0127) ─────

    def _engine_budget(self) -> int:
        """Characters one summary call may carry, from the SUMMARIZER's own window (not
        this turn's model's — the summary call runs on the summarizer)."""
        return compaction_engine.summarizer_budget_chars(compaction_engine.window_of_model(self.model))

    def _create_summary(self, messages_to_summarize):  # type: ignore[override]
        """Notes for the FULL span (no tail trim), via the engine. Raises on failure."""
        session_id, archived = _TURN_SESSION.get()
        return compaction_engine.summarize_span(
            list(messages_to_summarize or []),
            lambda prompt: self._summary_model.invoke(prompt, config=_summary_call_config()),
            budget_chars=self._engine_budget(),
            token_counter=self.token_counter,
            session_id=session_id,
            archived=archived,
        )

    async def _acreate_summary(self, messages_to_summarize):  # type: ignore[override]
        async def call(prompt):
            return await self._summary_model.ainvoke(prompt, config=_summary_call_config())

        session_id, archived = _TURN_SESSION.get()
        return await compaction_engine.asummarize_span(
            list(messages_to_summarize or []),
            call,
            budget_chars=self._engine_budget(),
            token_counter=self.token_counter,
            session_id=session_id,
            archived=archived,
        )

    @staticmethod
    def _build_new_messages(summary: str):  # type: ignore[override]
        return [compaction_engine.summary_message(summary, source="summarization")]

    # ── system-thread early compaction (#4625) ────────────────────────────────

    def _should_summarize(self, messages, total_tokens) -> bool:  # type: ignore[override]
        """The configured trigger, OR a much lower ceiling on ``system:*`` threads.

        A background wake resends its whole accumulated thread every time, so it needs to
        compact far earlier than a chat thread. The system ceiling is OR-ed with the
        parent's trigger exactly as ``compaction.max_tokens`` is (graph/agent.py): the
        effective threshold on a system thread is min(trigger, this). Chat threads only
        ever see the parent's decision, so their behaviour is unchanged. The system-thread
        flag is resolved once per hook (``before_model``) and read from ``_TURN_SYSTEM``.

        **Hysteresis (round-1 fix).** Compaction is keep-tail: it always preserves the
        newest ``keep`` messages whatever their size, plus the summary. So the kept tail +
        summary can themselves sit at or above the (low) system ceiling — and the ceiling
        also fires on the provider-*reported* total, which carries system-prompt and
        tool-schema overhead on top of the messages. With no gap between the trigger and
        the post-compaction size, a thread stuck there would re-compact on EVERY
        ``before_model`` of a wake — one summariser call, one archive entry and one
        compaction count per step — while trimming only the 1–2 newest messages. So the
        ceiling fires only when a compaction would actually shrink the thread by a
        meaningful margin (:meth:`_system_compaction_would_shrink`), measured from the
        real message tokens. The parent's own trigger and the absolute ``max_tokens``
        overflow valve are never gated this way — if either is met we always compact."""
        if super()._should_summarize(messages, total_tokens):
            return True
        ceiling = getattr(self, "_system_thread_max_tokens", 0)
        if not (ceiling and _TURN_SYSTEM.get()):
            return False
        # Mirror the ``max_tokens`` ceiling: fire on the computed OR the provider-reported
        # total. Either one only *proposes* a system compaction here; the shrink gate below
        # decides, so system-prompt/tool-schema overhead in the reported total can't by
        # itself force a churn.
        over_ceiling = total_tokens >= ceiling or self._should_summarize_based_on_reported_tokens(
            messages, float(ceiling)
        )
        return bool(over_ceiling) and self._system_compaction_would_shrink(messages, ceiling)

    def _system_ceiling_applies(self) -> bool:
        return bool(getattr(self, "_system_thread_max_tokens", 0) and _TURN_SYSTEM.get())

    def _determine_cutoff_index(self, messages) -> int:  # type: ignore[override]
        """The parent's keep-tail cutoff; on a ``system:*`` thread, never a kept tail of
        more than ``ceiling // _SYSTEM_KEEP_DIVISOR`` tokens (#5106).

        The later of the two cutoffs wins, so a system thread keeps the newest
        ``keep_messages`` only while they fit that budget, and at least its newest
        message (the parent's token cutoff never splits an AI/tool pair). Chat threads
        are unchanged. The hysteresis gate and the compaction itself both call this, so
        they always agree on what would be removed."""
        cutoff = super()._determine_cutoff_index(messages)
        if not self._system_ceiling_applies():
            return cutoff
        budget = max(1, self._system_thread_max_tokens // _SYSTEM_KEEP_DIVISOR)
        keep = self.keep
        try:
            self.keep = ("tokens", budget)
            by_tokens = self._find_token_based_cutoff(messages)
        except Exception:  # noqa: BLE001 — parent internals moved; keep its own cutoff
            log.debug("[compaction] system-thread token cutoff unavailable", exc_info=True)
            return cutoff
        finally:
            self.keep = keep
        return max(cutoff, by_tokens or 0)

    def _system_compaction_would_shrink(self, messages, ceiling: int) -> bool:
        """The hysteresis gate: would compacting a ``system:*`` thread right now summarise
        away enough to be worth the cost?

        A keep-tail compaction summarises ``messages[:cutoff]`` and keeps the rest; the
        tokens it removes are those of that head. Once a thread has been compacted, the
        only thing ahead of the kept tail is the previous summary (a few hundred tokens),
        so re-compacting would remove almost nothing — the churn the round-1 review found.
        Require the removable head to clear ``ceiling // _SYSTEM_HYSTERESIS_DIVISOR``: far
        larger than a leftover summary, so a just-compacted thread won't re-fire until it
        has accrued that much new history, yet small enough that the thread stays bounded
        near the ceiling. Measured from real message tokens, never the reported total."""
        try:
            cutoff = self._determine_cutoff_index(messages)
        except Exception:  # noqa: BLE001 — parent internals moved; don't disable the valve
            return True
        if cutoff <= 0:
            # The parent would compute the same cutoff and no-op, so there is nothing to do.
            return False
        # A previous compaction's notes are not new history: folding them into the next
        # summary shrinks nothing, so they never count toward the margin (#5106). The
        # engine's notes run to thousands of tokens (required sections + the operator's
        # messages verbatim), and counting them let one new tool result clear it.
        head = [m for m in messages[:cutoff] if not compaction_engine.is_summary_message(m)]
        if not head:
            return False
        try:
            removable = self.token_counter(head)
        except Exception:  # noqa: BLE001 — the full count already succeeded; be conservative
            return True
        return removable >= max(1, ceiling // _SYSTEM_HYSTERESIS_DIVISOR)

    # ── failure backoff ──────────────────────────────────────────────────────

    def _backoff_table(self) -> dict[str, dict]:
        table = getattr(self, "_backoff", None)
        if table is None:
            table = self._backoff = {}
        return table

    def _in_backoff(self, key: str) -> dict | None:
        import time

        entry = self._backoff_table().get(key)
        if entry is not None and time.monotonic() < entry["until"]:
            return entry
        return None

    def _record_failure(self, key: str, state, exc: BaseException) -> None:
        import time

        table = self._backoff_table()
        prev = table.pop(key, None)
        failures = (prev or {}).get("failures", 0) + 1
        delay = min(_BACKOFF_BASE_S * (2 ** (failures - 1)), _BACKOFF_MAX_S)
        table[key] = {
            "failures": failures,
            "until": time.monotonic() + delay,
            "error": f"{type(exc).__name__}: {exc}"[:500],
            "warned": False,
        }
        while len(table) > _BACKOFF_MAX_ENTRIES:
            table.pop(next(iter(table)))
        log.warning(
            "[compaction] summarization call FAILED for session %s (failure #%d) — continuing "
            "the turn WITHOUT compacting; auto-compaction on this thread is paused for %ds",
            str((state or {}).get("session_id") or "unknown"),
            failures,
            int(delay),
            exc_info=True,
        )

    def _record_success(self, key: str) -> None:
        self._backoff_table().pop(key, None)

    def _would_summarize(self, state) -> bool:
        try:
            messages = (state or {}).get("messages") or []
            return bool(self._should_summarize(messages, self.token_counter(messages)))
        except Exception:  # noqa: BLE001 — parent internals moved; just skip quietly
            return False

    def _skip_for_backoff(self, key: str, state) -> bool:
        """True while compaction is paused on this thread. Warns once per window."""
        entry = self._in_backoff(key)
        if entry is None:
            return False
        if not entry["warned"] and self._would_summarize(state):
            import time

            entry["warned"] = True
            log.warning(
                "[compaction] session %s is over the compaction trigger but auto-compaction is "
                "paused for %ds after %d failure(s) (last: %s) — history is growing uncompacted",
                str((state or {}).get("session_id") or "unknown"),
                int(entry["until"] - time.monotonic()),
                entry["failures"],
                entry["error"],
            )
        return True

    def _overflow_with_cause(self, request, exc: BaseException) -> BaseException | None:
        """For a context overflow on a thread whose compaction is failing, a clearer
        error naming that cause; otherwise None (the caller re-raises ``exc``)."""
        try:
            from graph.llm import is_context_overflow_error

            if not is_context_overflow_error(exc):
                return None
            entry = self._backoff_table().get(_backoff_key(getattr(request, "state", None)))
        except Exception:  # noqa: BLE001 — never mask the real error
            return None
        if entry is None:
            return None
        return CompactionFailedContextOverflow(
            f"{exc}\n\nThe conversation outgrew the model's context window because "
            f"auto-compaction has been failing on this thread ({entry['failures']} failure(s); "
            f"last: {entry['error']}). Fix that error (or run /compact) to recover."
        )

    def wrap_model_call(self, request, handler):
        try:
            return handler(request)
        except Exception as exc:
            clearer = self._overflow_with_cause(request, exc)
            if clearer is None:
                raise
            raise clearer from exc

    async def awrap_model_call(self, request, handler):
        try:
            return await handler(request)
        except Exception as exc:
            clearer = self._overflow_with_cause(request, exc)
            if clearer is None:
                raise
            raise clearer from exc

    # ── archive-first (#2784, ADR 0101 D5) ───────────────────────────────────

    def _archive(self, state, thread_id: str | None = None) -> None:
        """Archive the full pre-compaction transcript. Best-effort with the D5
        failure mode: any failure logs LOUDLY and compaction proceeds — never
        raises, never blocks the rewrite."""
        store = resolve_history_store(getattr(self, "_history_store", _DEFAULT_HISTORY_STORE))
        session_id = str((state or {}).get("session_id") or "unknown")
        if (state or {}).get("incognito"):
            log.info("[compaction] session %s is incognito — compacting WITHOUT an archive (ADR 0069 D3b)", session_id)
            return
        try:
            if store is None:
                log.warning(
                    "[compaction] no history store — auto-compacting session %s WITHOUT an "
                    "archive; the summarized-away history is unrecoverable (ADR 0101 D5)",
                    session_id,
                )
                return
            from graph.conversation_harvest import archive_payload

            content, kwargs = archive_payload(
                list((state or {}).get("messages") or []),
                session_id=session_id,
                thread_id=thread_id,
                cause="auto-compaction",
            )
            if not content:
                return  # nothing renderable — nothing to lose
            entry_id = store.add(content, **kwargs)
            if entry_id:
                log.info(
                    "[compaction] archived history entry %s for session %s before compacting",
                    entry_id,
                    session_id,
                )
            else:
                log.warning(
                    "[compaction] archive wrote no entry for session %s — compacting ANYWAY "
                    "(ADR 0101 D5): the summarized-away history is unrecoverable",
                    session_id,
                )
        except Exception:  # noqa: BLE001 — D5: loud, never blocking
            log.exception(
                "[compaction] archive FAILED for session %s — compacting ANYWAY (ADR 0101 D5): "
                "the summarized-away history is unrecoverable",
                session_id,
            )

    # ── hooks ────────────────────────────────────────────────────────────────

    def before_model(self, state, runtime):  # type: ignore[override]
        token = self._bind_turn_window(state)
        sys_token = _TURN_SYSTEM.set(_is_system_thread(state))
        session_token = _TURN_SESSION.set(_turn_session(state))
        try:
            return self._before_model(state, runtime)
        finally:
            _TURN_SESSION.reset(session_token)
            _TURN_SYSTEM.reset(sys_token)
            _TURN_WINDOW.reset(token)

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        token = self._bind_turn_window(state)
        sys_token = _TURN_SYSTEM.set(_is_system_thread(state))
        session_token = _TURN_SESSION.set(_turn_session(state))
        try:
            return await self._abefore_model(state, runtime)
        finally:
            _TURN_SESSION.reset(session_token)
            _TURN_SYSTEM.reset(sys_token)
            _TURN_WINDOW.reset(token)

    def _before_model(self, state, runtime):
        key = _backoff_key(state)
        if self._skip_for_backoff(key, state):
            return None
        try:
            result = super().before_model(state, runtime)
        except Exception as exc:  # noqa: BLE001 — compaction must never fail the user's turn
            if _is_control_flow(exc):
                raise
            self._record_failure(key, state, exc)
            return None
        if result is not None:
            result = _strip_retained_thinking(result)
            self._record_success(key)
            # The rewrite lands only when this update is RETURNED — archiving here
            # is before-commit, exactly like the manual path's ordering.
            self._archive(state, _thread_id())
            _count()
            _surface_op(state, result)
        return result

    async def _abefore_model(self, state, runtime):
        key = _backoff_key(state)
        if self._skip_for_backoff(key, state):
            return None
        try:
            result = await super().abefore_model(state, runtime)
        except Exception as exc:  # noqa: BLE001 — compaction must never fail the user's turn
            if _is_control_flow(exc):
                raise
            self._record_failure(key, state, exc)
            return None
        if result is not None:
            result = _strip_retained_thinking(result)
            self._record_success(key)
            import asyncio

            # add_document does blocking gateway work (embed/enrich) — off-loop,
            # same pattern as compaction_op / conversation_harvest.
            await asyncio.to_thread(self._archive, state, _thread_id())
            _count()
            _surface_op(state, result)
        return result
