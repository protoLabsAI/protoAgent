"""Knowledge-store op latency (#2676) — instrumentation point #6 from #2245.

``knowledge/hybrid_store.py`` times its three hot paths with ``time.monotonic()``
deltas — query (RRF hybrid search), ingest (add_document / add_chunk), embed
(embed_fn round-trips) — and emits them through
``observability.metrics.record_knowledge_op`` to the
``*_knowledge_op_seconds{op=...}`` histogram. Ops that run inside an A2A turn
additionally fold into that turn's TelemetryStore row via the executor's
contextvar accumulator, under ``knowledge:{op}`` keys in the SAME per-tool
durations blob as tool calls (#2697) — no new column.
"""

from __future__ import annotations

import json

import pytest

from knowledge.hybrid_store import HybridKnowledgeStore
from observability import metrics

_VOCAB = ["calculator", "math", "weather", "forecast", "python", "async"]


def _bow_embed(text: str) -> list[float]:
    """Deterministic bag-of-words embedding over a tiny vocab."""
    t = text.lower()
    return [1.0 if w in t else 0.0 for w in _VOCAB]


def _db(tmp_path):
    return str(tmp_path / "kb.db")


def _capture_ops(monkeypatch) -> list[tuple[str, float]]:
    calls: list[tuple[str, float]] = []
    monkeypatch.setattr(metrics, "record_knowledge_op", lambda op, duration_s: calls.append((op, duration_s)))
    return calls


# ── store-side timing ────────────────────────────────────────────────────────


def test_search_times_the_query_and_its_inner_embed(tmp_path, monkeypatch):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    store.add_chunk("use the calculator for math", domain="general")
    calls = _capture_ops(monkeypatch)

    results = store.search("math calculator")

    assert results  # instrumentation must not change behavior
    ops = [op for op, _ in calls]
    # The embed sample rides INSIDE the query sample by design — the split shows
    # how much of a slow query was the embedding round-trip.
    assert ops.count("query") == 1
    assert ops.count("embed") == 1
    assert all(d >= 0 for _, d in calls)


def test_empty_query_fast_path_is_untimed(tmp_path, monkeypatch):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    calls = _capture_ops(monkeypatch)
    assert store.search("   ") == []
    assert calls == []  # no op happened, so no sample


def test_add_chunk_times_one_ingest(tmp_path, monkeypatch):
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    calls = _capture_ops(monkeypatch)

    cid = store.add_chunk("tomorrow's weather forecast", domain="general")

    assert cid is not None
    ops = [op for op, _ in calls]
    assert ops.count("ingest") == 1
    assert ops.count("embed") == 1


def test_add_document_times_one_ingest_not_one_per_chunk(tmp_path, monkeypatch):
    # Per-chunk fallback path (no batched embedder): the document-level timer
    # owns the single ingest sample; the nested add_chunk calls are guarded.
    store = HybridKnowledgeStore(_db(tmp_path), embed_fn=_bow_embed)
    calls = _capture_ops(monkeypatch)

    ids = store.add_document(
        "the weather forecast says python async math calculator. " * 20,
        domain="general",
        max_chars=200,
        overlap_chars=0,
        min_chars=50,
    )

    assert len(ids) > 1, "test needs a genuinely multi-chunk document"
    ops = [op for op, _ in calls]
    assert ops.count("ingest") == 1
    assert ops.count("embed") == len(ids)  # one embed round-trip per chunk


def test_batched_add_document_times_one_ingest_and_one_embed(tmp_path, monkeypatch):
    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=_bow_embed,
        embed_batch_fn=lambda texts: [_bow_embed(t) for t in texts],
    )
    calls = _capture_ops(monkeypatch)

    ids = store.add_document(
        "the weather forecast says python async math calculator. " * 20,
        domain="general",
        max_chars=200,
        overlap_chars=0,
        min_chars=50,
    )

    assert len(ids) > 1
    ops = [op for op, _ in calls]
    assert ops.count("ingest") == 1
    assert ops.count("embed") == 1  # ADR 0021: the whole document is one batched round-trip


def test_failed_embed_is_timed_but_an_open_breaker_is_not(tmp_path, monkeypatch):
    # A FAILED embed is precisely the sample worth having (a transport timeout);
    # a breaker-open short-circuit does no embedding work, so no sample.
    def flaky_embed(text):
        raise RuntimeError("embedding service down")

    store = HybridKnowledgeStore(
        _db(tmp_path),
        embed_fn=flaky_embed,
        breaker_threshold=1,  # first failure opens the breaker
        breaker_cooldown_s=999,
    )
    store.add_chunk("use the calculator for math", domain="general")  # opens the breaker
    assert store._breaker_open()
    calls = _capture_ops(monkeypatch)

    store.search("calculator")  # breaker open → FTS5 only, embed short-circuits

    ops = [op for op, _ in calls]
    assert ops.count("query") == 1
    assert ops.count("embed") == 0


# ── metrics seam ─────────────────────────────────────────────────────────────


def test_record_knowledge_op_labels_the_op(monkeypatch):
    # Pin the label name the dashboards will query — {op=query|ingest|embed}.
    class FakeHist:
        def __init__(self):
            self.observed: list[tuple[dict, float]] = []
            self._labels: dict = {}

        def labels(self, **kw):
            self._labels = kw
            return self

        def observe(self, value):
            self.observed.append((dict(self._labels), value))

    fake = FakeHist()
    monkeypatch.setattr(metrics, "_enabled", True)
    monkeypatch.setattr(metrics, "_knowledge_op_latency", fake)
    metrics.record_knowledge_op("query", 0.02)
    assert fake.observed == [({"op": "query"}, 0.02)]


def test_record_knowledge_op_noops_when_metrics_disabled(monkeypatch):
    monkeypatch.setattr(metrics, "_enabled", False)
    metrics.record_knowledge_op("ingest", 0.1)  # must not raise


# ── per-turn attribution ─────────────────────────────────────────────────────


def test_turn_accumulator_collects_ops_in_ms():
    token = metrics.begin_knowledge_turn()
    try:
        metrics.record_knowledge_op("query", 0.25)
        metrics.record_knowledge_op("embed", 0.004)
        metrics.record_knowledge_op("query", 1.5)
        assert metrics.current_knowledge_ops() == {"query": [250, 1500], "embed": [4]}
    finally:
        ops = metrics.end_knowledge_turn(token)
    assert ops == {"query": [250, 1500], "embed": [4]}
    assert metrics.current_knowledge_ops() is None  # disarmed


def test_ops_outside_a_turn_do_not_accumulate():
    metrics.record_knowledge_op("query", 0.1)  # no armed turn — must not raise
    assert metrics.current_knowledge_ops() is None


@pytest.mark.asyncio
async def test_knowledge_ops_inside_a_turn_merge_into_the_outcome_durations():
    """End-to-end through the real executor: an op recorded mid-stream lands on
    the TurnOutcome's durations blob under a ``knowledge:{op}`` key, exactly like
    a tool call — that blob is what server/a2a.py writes to the telemetry row."""
    from a2a.server.agent_execution import RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.server.events.event_queue import EventQueueLegacy as EventQueue
    from a2a.types import Message, Part, Role, SendMessageRequest

    from a2a_impl.executor import ProtoAgentExecutor, TurnOutcome, set_terminal_hook

    async def stream(text, ctx, **kwargs):
        # A knowledge op fires mid-turn (recall before a model call, or inside a
        # tool body) — the executor's contextvar accumulator must catch it.
        metrics.record_knowledge_op("query", 0.123)
        yield ("text", "hello")
        yield ("done", "hello")

    seen: list[TurnOutcome] = []
    set_terminal_hook(seen.append)
    try:
        req = SendMessageRequest(message=Message(message_id="m-1", role=Role.ROLE_USER, parts=[Part(text="hi")]))
        ctx = RequestContext(call_context=ServerCallContext(), request=req, task_id="t-1", context_id="c-1")
        await ProtoAgentExecutor(stream).execute(ctx, EventQueue())
    finally:
        set_terminal_hook(None)

    assert len(seen) == 1
    assert seen[0].tool_durations.get("knowledge:query") == [123]
    assert metrics.current_knowledge_ops() is None  # the turn disarmed its accumulator


def test_knowledge_ops_flow_into_the_by_tool_percentiles(tmp_path):
    # The pseudo-tool key rides the existing #2697 pipeline end to end: a row
    # whose blob carries knowledge:query shows up in summary()'s by_tool split.
    from observability.telemetry_store import TelemetryStore

    store = TelemetryStore(str(tmp_path / "telemetry.db"))
    store.record(
        {
            "task_id": "t-1",
            "state": "completed",
            "ended_at": "2026-08-15T00:00:00+00:00",
            "tool_durations": json.dumps({"knowledge:query": [12, 40], "task": [900]}),
        }
    )
    by_tool = {r["tool"]: r for r in store.summary()["by_tool"]}
    assert by_tool["knowledge:query"]["calls"] == 2
    assert by_tool["knowledge:query"]["p50_duration_ms"] in (12, 40)
