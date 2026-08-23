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

Best-effort throughout — a telemetry failure must never affect a turn.
"""

from __future__ import annotations

import logging
import uuid

from runtime.state import STATE

log = logging.getLogger(__name__)

__all__ = ["record_turn", "local_task_id"]


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
    show live spend without polling the store. Independent of the SQL store."""
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

    input_tokens = int(u.get("input_tokens", 0) or 0)
    output_tokens = int(u.get("output_tokens", 0) or 0)
    primary_model = models[0] if models else ((STATE.graph_config.model_name if STATE.graph_config else "") or "")

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
        "total_tokens": input_tokens + output_tokens,
        "cache_read_input_tokens": int(u.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_input_tokens": int(u.get("cache_creation_input_tokens", 0) or 0),
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
        _publish_usage(row, models, soul_rev)

    store = STATE.telemetry_store
    if store is None:
        return
    try:
        store.record(row)
    except Exception:  # noqa: BLE001 — telemetry is best-effort
        log.exception("[telemetry] failed to record turn %s", task_id)
