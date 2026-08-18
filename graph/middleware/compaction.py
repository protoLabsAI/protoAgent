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

2. **A Prometheus counter** on each real compaction (ADR 0006 — proves the
   lever fires, and how often).

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


def _count() -> None:
    try:
        from observability import metrics

        metrics.record_compaction()
    except Exception:  # noqa: BLE001 — telemetry must never break a model call
        pass


class CountingSummarizationMiddleware(SummarizationMiddleware):
    """``SummarizationMiddleware`` + archive-first (#2784) + a compaction counter."""

    def __init__(self, *args, knowledge_store=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._knowledge_store = knowledge_store

    # ── archive-first (#2784, ADR 0101 D5) ───────────────────────────────────

    def _archive(self, state) -> None:
        """Archive the full pre-compaction transcript. Best-effort with the D5
        failure mode: any failure logs LOUDLY and compaction proceeds — never
        raises, never blocks the rewrite."""
        store = getattr(self, "_knowledge_store", None)
        session_id = str((state or {}).get("session_id") or "unknown")
        try:
            if store is None:
                log.warning(
                    "[compaction] no knowledge store — auto-compacting session %s WITHOUT an "
                    "archive; the summarized-away history is unrecoverable (ADR 0101 D5)",
                    session_id,
                )
                return
            from graph.conversation_harvest import render_transcript
            from knowledge import add_document

            transcript = render_transcript(list((state or {}).get("messages") or []), max_chars=None)
            if not transcript.strip():
                return  # nothing renderable — nothing to lose
            chunk_ids = add_document(
                store,
                transcript,
                domain="conversation",
                heading=f"Conversation archive (auto-compaction, {session_id})",
                source_type="conversation",
                namespace=f"chat-archive:{session_id}",
            )
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
        result = super().before_model(state, runtime)
        if result is not None:
            # The rewrite lands only when this update is RETURNED — archiving here
            # is before-commit, exactly like the manual path's ordering.
            self._archive(state)
            _count()
            _surface_op(state, result)
        return result

    async def abefore_model(self, state, runtime):  # type: ignore[override]
        result = await super().abefore_model(state, runtime)
        if result is not None:
            import asyncio

            # add_document does blocking gateway work (embed/enrich) — off-loop,
            # same pattern as compaction_op / conversation_harvest.
            await asyncio.to_thread(self._archive, state)
            _count()
            _surface_op(state, result)
        return result
