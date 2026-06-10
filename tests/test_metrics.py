"""The A2A-turn metrics emit safely (prod-readiness: /metrics can alert on a
failing/backed-up agent) and never break a turn."""

from observability import metrics


def test_record_a2a_turn_safe_when_disabled():
    # No init() — must be a no-op, never raise (metrics are optional).
    metrics.record_a2a_turn("completed", 1.5)
    metrics.record_a2a_turn("failed")
    metrics.record_a2a_turn("", None)


def test_record_a2a_turn_increments_when_enabled():
    if metrics.is_enabled() or _try_init():
        from prometheus_client import REGISTRY
        p = metrics._prefix()
        before = REGISTRY.get_sample_value(f"{p}_a2a_turns_total", {"state": "completed"}) or 0.0
        metrics.record_a2a_turn("completed", 2.0)
        after = REGISTRY.get_sample_value(f"{p}_a2a_turns_total", {"state": "completed"}) or 0.0
        assert after == before + 1.0


def _try_init() -> bool:
    try:
        metrics.init()
        return metrics.is_enabled()
    except Exception:
        return False  # already-registered or prometheus missing — skip
