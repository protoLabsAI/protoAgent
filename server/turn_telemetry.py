"""The one place a finished turn becomes a telemetry row (ADR 0006, #3000).

protoAgent has two turn drivers, and only one of them used to be instrumented.
``_chat_langgraph_stream`` (the A2A executor's producer) reached the telemetry
store through the executor's terminal hook; ``_chat_langgraph`` — the
non-streaming driver behind the OpenAI-compatible ``/v1/chat/completions``,
``/api/chat``, and the ADR 0018 plugin ``HOST.invoke()`` seam — reached nothing
at all. Those turns spent real tokens and appeared in no store row, no
Prometheus sample, and no ``turn.usage`` bus event, so every cost total, success
rate, and latency percentile silently described a subset of real traffic with no
indication that it was a subset.

Both drivers now call :func:`record_turn`. It is deliberately a function taking
plain fields rather than a ``TurnOutcome``: the non-streaming path has no such
object, and typing the seam to one driver's data structure is how the two came
apart in the first place.

A THIRD producer joins them, and it is not a turn driver at all: ``AcpClient.prompt``
(#3015) records one row per CLI coding-agent run under a ``coder:`` key. Those runs are
dispatched from a plugin's background loop rather than from a turn, which is exactly why
they were invisible until they were routed through here. If you are adding a new surface
that spends tokens, this function is the seam — anything that does not reach it is, by
construction, unmeasured.

The non-streaming driver's side of that seam lives here too (#3810): the usage
callback it attaches to a turn (:func:`make_usage_callback`), the fold of that
callback's per-model totals into a row (:func:`telemetry_usage`) and into the OpenAI
wire ``usage`` shape (:func:`sum_usage`), and the row writer itself
(:func:`record_local_turn`). ``server.chat`` re-exports them under their historical
private names (``_record_local_turn`` …) — patch them HERE, not there.

Best-effort throughout — a telemetry failure must never affect a turn.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from runtime.state import STATE
from tools.a2a_parse import drop_peer_markers

log = logging.getLogger(__name__)

__all__ = [
    "local_task_id",
    "make_usage_callback",
    "record_local_turn",
    "record_turn",
    "sum_usage",
    "telemetry_usage",
]


def local_task_id(origin: str) -> str:
    """A row key for a turn that has no A2A task.

    The store needs a non-empty ``task_id``, and the non-streaming surfaces have
    no task to name. The ``origin`` prefix is the point: without it a row from
    ``/v1`` is indistinguishable from one the console produced, and "which
    surface is spending this" is exactly the question these rows exist to answer.
    """
    return f"{origin or 'local'}:{uuid.uuid4().hex[:12]}"


def _soul_revision() -> str:
    """Which persona (SOUL.md) was live for this turn (#1691).

    Deliberately NOT a Prometheus label — a content hash is high-cardinality and
    would explode the series.
    """
    try:
        from graph.config_io import soul_revision

        return soul_revision()
    except Exception:  # noqa: BLE001 — telemetry must never break a turn
        return ""


def _publish_usage(row: dict, models: list[str], soul_rev: str) -> None:
    """Realtime cost/usage on the bus (ADR 0051 Slice 3) so a per-turn HUD can
    show live spend without polling the store. Independent of the SQL store.

    ``models`` is the turn's REAL models — the caller has already dropped the
    ``peer:`` markers (#3016), which name a delegate rather than a model."""
    try:
        from server import _event_bus

        _event_bus.publish(
            "turn.usage",
            {
                "task_id": row.get("task_id", ""),
                "context_id": row.get("session_id", ""),
                "state": row.get("state", ""),
                # The turn's ACTUAL lead model, empty when the turn made no model
                # call — NOT the configured default the store row falls back to.
                # The console reads this as "what ran", so inventing one would lie.
                "model": models[0] if models else "",
                "input_tokens": int(row.get("input_tokens", 0) or 0),
                "output_tokens": int(row.get("output_tokens", 0) or 0),
                "cost_usd": round(float(row.get("cost_usd", 0.0) or 0.0), 6),
                "duration_ms": int(row.get("duration_ms", 0) or 0),
                "soul_rev": soul_rev,
            },
        )
    except Exception:  # noqa: BLE001 — best-effort
        pass


def _success_for(state: str) -> int | None:
    """1 / 0 / NULL for the ``success`` column.

    A parked (``input_required``) leg is neither: it is a turn waiting on a
    human. NULL keeps it out of both halves of the success rate (#2943, #3004).
    """
    if state == "input_required":
        return None
    return 1 if state == "completed" else 0


def record_turn(
    *,
    task_id: str,
    session_id: str,
    state: str,
    models: list[str] | None = None,
    usage: dict | None = None,
    cost_usd: float = 0.0,
    duration_ms: int = 0,
    llm_calls: int = 0,
    tool_calls: int = 0,
    trace_id: str = "",
    tool_durations: dict | None = None,
    context_tokens: int = 0,
    publish_usage_event: bool = True,
) -> None:
    """Record one finished turn leg: Prometheus, the realtime bus, and the store.

    Every step is independently guarded, so a failure in one still lets the
    others run and none of them can reach the caller.

    ``publish_usage_event=False`` writes the durable row and the Prometheus
    sample but skips the ``turn.usage`` bus event. The console's fleet roster
    counts a member "running" with a strict +1 on ``turn.started`` and −1 on the
    terminal ``turn.usage``; a second producer emitting the −1 half without the
    +1 would corrupt that count. Wiring the non-streaming surfaces into the live
    HUD is its own change with its own contract to settle (#3000).
    """
    import json
    from datetime import datetime, timedelta, timezone

    models = list(models or [])
    u = usage or {}
    duration_ms = int(duration_ms or 0)

    # Prometheus turn counter — independent of the SQL store, so /metrics can
    # alert on a failing or backed-up agent without scraping SQLite.
    try:
        from observability import metrics

        metrics.record_a2a_turn(state, duration_ms / 1000.0)
    except Exception:  # noqa: BLE001 — the metric must never break a turn
        pass

    soul_rev = _soul_revision()

    # Normalise the prompt split so the stored columns are DISJOINT (#3003).
    #
    # LangChain's `usage_metadata` — the shape every producer here reads — defines
    # `input_tokens` as "the sum of all input token types", with cache reads and
    # cache writes as SUBSETS of it. Carrying that shape into the store made
    # `input_tokens` mean "all prompt tokens" on one row and left every consumer to
    # guess whether adding the cache columns double-counts. It does. So the column
    # is stored cache-EXCLUSIVE: uncached + cache_read + cache_creation is the
    # turn's true prompt size, and each part is billed at its own rate.
    #
    # Rows written before this change keep the old cache-inclusive meaning; they
    # are not backfilled, so a `cache_hit_ratio` computed over pre-#3003 history
    # reads slightly low. Deliberate — a silent rewrite of recorded history is
    # worse than a documented seam.
    cache_read = int(u.get("cache_read_input_tokens", 0) or 0)
    cache_creation = int(u.get("cache_creation_input_tokens", 0) or 0)
    # Clamped: a provider that reports cache counts NOT included in `input_tokens`
    # (against the documented contract) must not drive this negative.
    input_tokens = max(0, int(u.get("input_tokens", 0) or 0) - cache_read - cache_creation)
    output_tokens = int(u.get("output_tokens", 0) or 0)
    # The `model` column names the model that ran this turn, so it must skip the
    # `peer:<delegate>` markers a delegation contributes (#3016); `models` below keeps
    # them, which is where peer spend stays legible. See `drop_peer_markers` for why.
    real_models = drop_peer_markers(models)
    configured_model = (STATE.graph_config.model_name if STATE.graph_config else "") or ""
    primary_model = real_models[0] if real_models else configured_model

    ended = datetime.now(timezone.utc)
    created = ended - timedelta(milliseconds=duration_ms)
    row = {
        "task_id": task_id,
        "session_id": session_id,
        "state": state,
        "success": _success_for(state),
        "model": primary_model,
        "models": ",".join(models),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        # Every token the turn moved. Must re-add the cache components, since
        # `input_tokens` above is now the UNCACHED share only (#3003) — summing
        # just input+output would silently drop the cached prompt from the total.
        "total_tokens": input_tokens + cache_read + cache_creation + output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "cost_usd": float(cost_usd or 0.0),
        "duration_ms": duration_ms,
        "llm_calls": int(llm_calls or 0),
        "tool_calls": int(tool_calls or 0),
        "created_at": created.isoformat(),
        "ended_at": ended.isoformat(),
        "soul_rev": soul_rev,
        "trace_id": trace_id or "",
        # Per-tool durations, tool name → [ms, ...] (#2697). JSON, not comma-joined
        # like `models`: durations need real structure (name AND numbers).
        "tool_durations": json.dumps(tool_durations) if tool_durations else None,
        # Peak single-call prompt size = context-window fill (#2773, ADR 0101 D6).
        "context_tokens": int(context_tokens or 0),
    }

    if publish_usage_event:
        _publish_usage(row, real_models, soul_rev)

    store = STATE.telemetry_store
    if store is None:
        return
    try:
        store.record(row)
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        log.exception("[telemetry] failed to record turn %s", task_id)


def record_local_turn(sink: dict, *, session_id: str, origin: str, state: str, started: float) -> None:
    """Write the telemetry row for one non-streaming turn (#3000). Best-effort.

    ``sink`` is populated by ``_chat_langgraph_impl`` (``server/turn_sync.py``) with the turn's usage
    callback. It stays empty when the turn short-circuited before reaching the
    graph — a `/help` command, an unknown slash command, "setup not complete", a
    HITL hold. Those spend nothing, so they get no row: a telemetry surface that
    counts control-plane replies as turns is worse than one that doesn't.

    A FAILED turn is the exception (#3929): it is always recorded, with whatever
    usage it managed (zero when the provider rejected the first call — a 400/429
    before any usage — or when it died before the graph). Skipping it made failed
    ``/v1`` and ``/api/chat`` turns invisible in ``/api/telemetry/recent`` while
    failed A2A turns showed up as ``failed``, so the success rate over-reported.
    ``state`` is ``failed`` only when the reply carries the structured ``error`` key
    (#3914's contract) or the impl raised — control-plane replies never carry it.
    """
    try:
        usage_cb = sink.get("usage_cb")
        failed = state == "failed"
        if usage_cb is None and not failed:
            return
        per_model = (getattr(usage_cb, "usage_metadata", None) or {}) if usage_cb is not None else {}
        models, usage, cost = telemetry_usage(per_model)
        if not failed and not models and not usage["input_tokens"] and not usage["output_tokens"]:
            return  # reached the graph but made no model call (an ACP turn, a tool-only short-circuit)

        from observability import tracing

        record_turn(
            task_id=local_task_id(origin),
            session_id=session_id,
            state=state,
            models=models,
            usage=usage,
            cost_usd=cost,
            duration_ms=int((time.monotonic() - started) * 1000),
            llm_calls=int(getattr(usage_cb, "llm_calls", 0) or 0),
            tool_calls=int(getattr(usage_cb, "tool_calls", 0) or 0),
            trace_id=tracing.current_trace_id() or "",
            # No per-call breakdown on this path: LangChain's usage callback
            # aggregates PER MODEL across the turn, so the peak single-call prompt
            # size (context fill) and per-tool durations aren't recoverable from it.
            # Left at their empty values rather than filled with a plausible-looking
            # number derived from the wrong thing.
            context_tokens=0,
            tool_durations=None,
            # See record_turn: the fleet roster's running count pairs a +1 on
            # turn.started with a -1 on the terminal turn.usage, and this driver
            # emits no turn.started.
            publish_usage_event=False,
        )
    except Exception:  # noqa: BLE001 — telemetry must never break a turn
        log.debug("[telemetry] failed to record a non-streaming turn", exc_info=True)


def make_usage_callback():
    """LangChain's per-model usage collector, plus the call counts a telemetry row
    needs (#3000).

    Subclassed rather than attached as a SECOND handler on purpose: the goal
    continuation re-attaches this object explicitly by name
    (``callbacks: [usage_cb]``), so a separate counter handler would have to be
    remembered there too — and the one that got forgotten would undercount
    silently. One object, one attachment site to keep right.
    """
    from langchain_core.callbacks import UsageMetadataCallbackHandler

    class _TurnUsageCallback(UsageMetadataCallbackHandler):
        def __init__(self) -> None:
            super().__init__()
            self.llm_calls = 0
            self.tool_calls = 0

        def on_llm_end(self, *args, **kwargs):
            # The base does the real work here (folding usage_metadata per model),
            # so forward whatever we were handed, unexamined.
            self.llm_calls += 1
            return super().on_llm_end(*args, **kwargs)

        def on_tool_end(self, *args, **kwargs):
            # Deliberately does NOT call super(). The base's `on_tool_end` is an
            # empty stub whose signature requires a keyword-only `run_id`, so
            # delegating buys nothing and couples a telemetry counter to a
            # signature that can raise inside a live turn's callback path.
            self.tool_calls += 1
            return None

    return _TurnUsageCallback()


def telemetry_usage(per_model: dict[str, Any]) -> tuple[list[str], dict[str, int], float]:
    """Fold LangChain's per-model ``usage_metadata`` into the telemetry-row shape:
    ``(models, summed usage, cost_usd)`` (#3000).

    Distinct from :func:`sum_usage`, which produces the OpenAI wire shape and drops
    the cache fields. Cost is summed PER MODEL rather than computed once on the
    totals — a turn that routed across a pinned subagent and the lead bills each at
    its own rate, and collapsing them first would price the whole turn at whichever
    model happened to be listed.
    """
    from observability import pricing

    models = list(per_model or {})
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    cost = 0.0
    for model, u in (per_model or {}).items():
        u = u or {}
        details = u.get("input_token_details") or {}
        one = {
            "input_tokens": int(u.get("input_tokens", 0) or 0),
            "output_tokens": int(u.get("output_tokens", 0) or 0),
            "cache_read_input_tokens": int(details.get("cache_read", 0) or 0),
            "cache_creation_input_tokens": int(details.get("cache_creation", 0) or 0),
        }
        for k, v in one.items():
            totals[k] += v
        cost += pricing.cost_usd(model, one)
    return models, totals, round(cost, 6)


def sum_usage(per_model: dict[str, Any]) -> dict[str, int]:
    """Fold LangChain's per-model ``usage_metadata`` (``{input,output,total}_tokens``) into
    the OpenAI ``usage`` shape, summed across every model call in the turn — the lead model
    plus any aux/fallback/subagent calls. Powers the /v1 OpenAI-compat ``usage`` field
    (ADR 0075 D4); ``/api/chat`` ignores the extra key. ``total`` falls back to
    prompt+completion for gateways that omit it."""
    prompt = sum(int((u or {}).get("input_tokens", 0) or 0) for u in per_model.values())
    completion = sum(int((u or {}).get("output_tokens", 0) or 0) for u in per_model.values())
    total = sum(int((u or {}).get("total_tokens", 0) or 0) for u in per_model.values())
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total or (prompt + completion),
    }
