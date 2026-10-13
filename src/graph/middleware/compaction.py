"""SummarizationMiddleware that archives before it compacts, and counts.

langchain's ``SummarizationMiddleware`` summarizes old history near the context
limit. Its ``before_model`` / ``abefore_model`` hooks return a non-``None`` state
update **only** when they actually compact (otherwise ``None``). We subclass for
two additions:

1. **Archive-first (#2784, ADR 0101 D5).** Auto-compaction was the LOSSY path:
   the rewrite landed in the checkpoint, ``checkpoint_keep_per_thread`` pruning
   destroyed the pre-compaction rows, and the summarized-away history was simply
   gone — while the never-lossy manual ``/compact`` archived first. Now, when the
   parent decides to compact, the full transcript is archived to the knowledge
   store (``chat-archive:<session_id>``, the same namespace ``/compact`` uses)
   BEFORE the update is returned — i.e. before anything is committed. Failure
   mode, operator-decided: attempt the archive; on failure, compact ANYWAY with
   a loud log and let the safety valve do its duty — this is the automatic path
   between the model and an overflow error, and purity loses to availability
   here. The manual path keeps its strict refusal.

   The archive row carries ``source=<thread>`` and the dates of the messages it
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

Telemetry and archiving are both best-effort: neither ever affects the model call.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import SummarizationMiddleware

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


def _thread_id() -> str | None:
    """The checkpoint thread this turn runs on, for the archive's ``source``. The
    hooks run inside a graph node, so the run's config carries it; None outside a run."""
    try:
        from langgraph.config import get_config

        tid = ((get_config() or {}).get("configurable") or {}).get("thread_id")
    except Exception:  # noqa: BLE001 — no run context (a unit test calling the hook)
        return None
    return str(tid) if tid else None


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


class CountingSummarizationMiddleware(SummarizationMiddleware):
    """``SummarizationMiddleware`` + archive-first (#2784) + a compaction counter."""

    def __init__(self, *args, knowledge_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._knowledge_store = knowledge_store
        # Replace the parent's retry-everything ``with_retry()`` wrapper.
        self._summary_model = _TransientRetry(self.model)
        self._backoff: dict[str, dict] = {}

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
        store = getattr(self, "_knowledge_store", None)
        session_id = str((state or {}).get("session_id") or "unknown")
        if (state or {}).get("incognito"):
            log.info("[compaction] session %s is incognito — compacting WITHOUT an archive (ADR 0069 D3b)", session_id)
            return
        try:
            if store is None:
                log.warning(
                    "[compaction] no knowledge store — auto-compacting session %s WITHOUT an "
                    "archive; the summarized-away history is unrecoverable (ADR 0101 D5)",
                    session_id,
                )
                return
            from graph.conversation_harvest import archive_payload
            from knowledge import add_document

            content, kwargs = archive_payload(
                list((state or {}).get("messages") or []),
                session_id=session_id,
                thread_id=thread_id,
                cause="auto-compaction",
            )
            if not content:
                return  # nothing renderable — nothing to lose
            chunk_ids = add_document(store, content, **kwargs)
            if chunk_ids:
                log.info(
                    "[compaction] archived %d chunk(s) for session %s before compacting",
                    len(chunk_ids),
                    session_id,
                )
            else:
                log.warning(
                    "[compaction] archive wrote no chunks for session %s — compacting ANYWAY "
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
            self._record_success(key)
            # The rewrite lands only when this update is RETURNED — archiving here
            # is before-commit, exactly like the manual path's ordering.
            self._archive(state, _thread_id())
            _count()
            _surface_op(state, result)
        return result

    async def abefore_model(self, state, runtime):  # type: ignore[override]
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
            self._record_success(key)
            import asyncio

            # add_document does blocking gateway work (embed/enrich) — off-loop,
            # same pattern as compaction_op / conversation_harvest.
            await asyncio.to_thread(self._archive, state, _thread_id())
            _count()
            _surface_op(state, result)
        return result
