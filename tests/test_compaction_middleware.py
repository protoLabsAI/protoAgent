"""Tests for CountingSummarizationMiddleware (ADR 0006 — compaction signal).

The subclass must emit a metrics counter exactly when the parent actually
compacts (returns a non-None state update) — and never when it returns None.
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware import SummarizationMiddleware

from observability import metrics
from graph.middleware.compaction import CountingSummarizationMiddleware


def _instance():
    # Skip the heavy __init__ (needs a model); we only exercise the override.
    return object.__new__(CountingSummarizationMiddleware)


def test_counts_when_parent_compacts(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics, "record_compaction", lambda: calls.append(1))
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    out = _instance().before_model({"messages": []}, None)
    assert out == {"messages": []}  # parent result passed through
    assert calls == [1]  # counted once


def test_no_count_when_parent_returns_none(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics, "record_compaction", lambda: calls.append(1))
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: None)
    assert _instance().before_model({}, None) is None
    assert calls == []


@pytest.mark.asyncio
async def test_async_counts_when_parent_compacts(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics, "record_compaction", lambda: calls.append(1))

    async def _fake(self, s, r):
        return {"messages": []}

    monkeypatch.setattr(SummarizationMiddleware, "abefore_model", _fake)
    out = await _instance().abefore_model({}, None)
    assert out == {"messages": []}
    assert calls == [1]


def test_record_compaction_noop_when_disabled():
    metrics.record_compaction()  # metrics disabled in tests → no-op, no error


# ── archive-first (#2784, ADR 0101 D5) ────────────────────────────────────────


def _kb():
    class _Store:
        pass

    return _Store()


def test_archives_before_a_real_compaction(monkeypatch):
    """When the parent compacts, the FULL pre-compaction transcript is archived
    to the same chat-archive namespace /compact uses — before the update lands."""
    from langchain_core.messages import HumanMessage

    docs = []
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    monkeypatch.setattr(
        "knowledge.add_document",
        lambda store, text, **kw: docs.append({"text": text, **kw}) or [1],
    )
    mw = _instance()
    mw._knowledge_store = _kb()
    state = {"messages": [HumanMessage(content="the sky is teal")], "session_id": "sessX"}
    assert mw.before_model(state, None) == {"messages": []}
    assert len(docs) == 1
    assert "teal" in docs[0]["text"]
    assert docs[0]["namespace"] == "chat-archive:sessX"
    assert docs[0]["domain"] == "conversation"


def test_archive_failure_never_blocks_the_compaction(monkeypatch, caplog):
    """ADR 0101 D5 (operator-decided): attempt the archive; on failure compact
    ANYWAY with a loud log — safety-valve duty outranks purity on the automatic
    path. The manual /compact keeps its strict refusal separately."""
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})

    def _boom(*a, **k):
        raise RuntimeError("store on fire")

    monkeypatch.setattr("knowledge.add_document", _boom)
    mw = _instance()
    mw._knowledge_store = _kb()
    with caplog.at_level("ERROR"):
        out = mw.before_model({"messages": [HumanMessage(content="m")], "session_id": "s"}, None)
    assert out == {"messages": []}  # the compaction still happened
    assert "compacting ANYWAY" in caplog.text


def test_no_store_compacts_with_a_loud_warning(monkeypatch, caplog):
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    mw = _instance()  # no _knowledge_store attribute at all (getattr default)
    with caplog.at_level("WARNING"):
        out = mw.before_model({"messages": [HumanMessage(content="m")], "session_id": "s"}, None)
    assert out == {"messages": []}
    assert "WITHOUT an archive" in caplog.text


def test_no_archive_when_parent_does_not_compact(monkeypatch):
    docs = []
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: None)
    monkeypatch.setattr("knowledge.add_document", lambda *a, **k: docs.append(1) or [1])
    mw = _instance()
    mw._knowledge_store = _kb()
    assert mw.before_model({"messages": []}, None) is None
    assert docs == []
