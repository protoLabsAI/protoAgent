"""SummarizationMiddleware that counts compaction events (ADR 0006).

langchain's ``SummarizationMiddleware`` summarizes old history near the context
limit. Its ``before_model`` / ``abefore_model`` hooks return a non-``None`` state
update **only** when they actually compact (otherwise ``None``). We subclass to
emit a Prometheus counter on each real compaction — proving the compaction lever
fires (and how often), the last unmeasured optimization lever in the flywheel.

Telemetry is best-effort: a metrics failure never affects summarization.
"""

from __future__ import annotations

from langchain.agents.middleware import SummarizationMiddleware


def _count() -> None:
    try:
        from observability import metrics

        metrics.record_compaction()
    except Exception:  # noqa: BLE001 — telemetry must never break a model call
        pass


class CountingSummarizationMiddleware(SummarizationMiddleware):
    """``SummarizationMiddleware`` + a compaction counter (ADR 0006)."""

    def before_model(self, state, runtime):  # type: ignore[override]
        result = super().before_model(state, runtime)
        if result is not None:
            _count()
        return result

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        result = await super().abefore_model(state, runtime)
        if result is not None:
            _count()
        return result
