"""Prune oversized tool results already in history — before summarization ever runs.

ADR 0101 D3/D4 (#2782). Every tool output is capped at CALL time, but once a
``ToolMessage`` lands in the checkpointer it is re-sent verbatim on every later
model call until compaction removes the *entire* history — an all-or-nothing
lever. This middleware adds the missing middle step, and the ORDER is the point
(DeepSeek Harness's compaction insight): prune (near-lossless — both ends of
each result survive) before summarize (lossy) is ever considered. A pass here
often relieves enough pressure that the 0.8 compaction valve never fires.

Mechanics:

- **Pressure-triggered, late by default.** Nothing happens until the estimated
  request size crosses ``pruning.at_fraction`` of the model's context window
  (chars//4 heuristic — same arithmetic as the meter; precision belongs to the
  adapter layer). Without a gateway window profile it degrades to a fixed
  conservative token estimate, mirroring compaction's messages-count fallback.
- **One batched pass, not continuous nibbling.** Every eligible result is
  rewritten in the same update. Each rewrite invalidates the rolling history
  breakpoints (#2777) once — batching amortizes that to one cache miss per
  pass instead of one per call.
- **Replacement by id.** Each pruned copy keeps its message id and
  ``tool_call_id``, so the ``add_messages`` reducer REPLACES the original in
  place — AIMessage/ToolMessage pairing is preserved by construction (the same
  mechanism ``tool_call_repair`` relies on).
- **The newest ``keep_messages`` messages are never touched** — recent results
  are what the model is actively working from.
- **Honest markers.** The stub names what was elided and why, and tells the
  model to re-run the tool if it needs the middle — the full text is NOT
  recoverable from history afterwards (the audit log keeps only a summary), and
  the marker must not pretend otherwise. Exports show the stub as-is.
"""

from __future__ import annotations

import logging

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

log = logging.getLogger(__name__)

# Conservative pressure floor when the gateway reports no context window for the
# model (est. tokens, chars//4): high enough that a normal session never trips
# it, low enough to matter before a 200k-window model actually hurts.
FALLBACK_TRIGGER_TOKENS = 80_000

# Stub shape: bounded head + marker + bounded tail (same convention as the MCP
# call-time cap, #2781) — results front-load answers, tails carry conclusions.
STUB_HEAD_CHARS = 1_000
STUB_TAIL_CHARS = 500

# Idempotence sentinel — a result already pruned is never pruned again.
_MARKER_SENTINEL = "chars pruned by protoAgent at context pressure"


def _est_tokens(messages) -> int:
    """chars//4 over every message's text content — the meter's arithmetic."""
    total = 0
    for m in messages:
        content = getattr(m, "content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    total += len(str(b.get("text") or b.get("thinking") or ""))
                elif isinstance(b, str):
                    total += len(b)
    return total // 4


def _stub(text: str, *, tool_name: str) -> str:
    omitted = len(text) - STUB_HEAD_CHARS - STUB_TAIL_CHARS
    marker = (
        f"\n\n[... {omitted:,} of {len(text):,} {_MARKER_SENTINEL} (#2782) — "
        f"this older `{tool_name}` result was kept head+tail only; the middle is "
        f"gone from history, so re-run the tool if you need it ...]\n\n"
    )
    return text[:STUB_HEAD_CHARS] + marker + text[-STUB_TAIL_CHARS:]


class ToolResultPrunerMiddleware(AgentMiddleware):
    """Rewrite oversized, older-than-recent tool results to head+tail stubs."""

    def __init__(
        self,
        *,
        max_input_tokens: int | None = None,
        at_fraction: float = 0.6,
        keep_messages: int = 20,
        min_chars: int = 4_000,
    ) -> None:
        super().__init__()
        self._max_input = max_input_tokens
        self._at = max(0.05, min(float(at_fraction), 1.0))
        self._keep = max(0, int(keep_messages))
        # Below this a rewrite saves less than its marker costs in attention.
        # Must exceed head+tail or a "pruned" result could GROW.
        self._min_chars = max(int(min_chars), STUB_HEAD_CHARS + STUB_TAIL_CHARS + 400)

    def _threshold_tokens(self) -> int:
        if self._max_input:
            return int(self._max_input * self._at)
        return FALLBACK_TRIGGER_TOKENS

    def before_model(self, state, runtime) -> dict | None:
        messages = state.get("messages") or []
        if len(messages) <= self._keep:
            return None
        est = _est_tokens(messages)
        if est < self._threshold_tokens():
            return None

        replacements = []
        saved_chars = 0
        for m in messages[: len(messages) - self._keep]:
            if not isinstance(m, ToolMessage):
                continue
            content = getattr(m, "content", None)
            if not isinstance(content, str) or len(content) <= self._min_chars:
                continue
            if _MARKER_SENTINEL in content:
                continue  # already pruned on an earlier pass
            stub = _stub(content, tool_name=getattr(m, "name", "") or "tool")
            saved_chars += len(content) - len(stub)
            replacements.append(m.model_copy(update={"content": stub}))

        if not replacements:
            return None

        log.info(
            "[pruning] context ~%dk est. tokens crossed the %.0f%% threshold — pruned %d tool "
            "result(s) older than the last %d messages in one batched pass (~%dk tokens relieved)",
            est // 1000,
            self._at * 100,
            len(replacements),
            self._keep,
            saved_chars // 4 // 1000,
        )
        try:
            from observability import metrics

            metrics.record_pruning(len(replacements))
        except Exception:  # noqa: BLE001 — telemetry must never break a model call
            pass
        try:
            from observability.trajectory import log_surface_op

            log_surface_op(
                str(state.get("session_id") or ""),
                "prune",
                cause=f"pressure>={self._at:.0%}",
                rewritten_ids=[m.id for m in replacements],
            )
        except Exception:  # noqa: BLE001
            pass
        return {"messages": replacements}
