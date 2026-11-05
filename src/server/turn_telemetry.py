"""Compatibility alias for the shared turn telemetry writer.

The bus adapter stays in server; graph producers use observability directly.
"""

import sys

from observability import turn_telemetry as _shared


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


# Keep historical imports and monkeypatches on the same module object.
_shared._publish_usage = _publish_usage
sys.modules[__name__] = _shared
