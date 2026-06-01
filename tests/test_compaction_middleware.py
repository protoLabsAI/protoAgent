"""Tests for CountingSummarizationMiddleware (ADR 0006 — compaction signal).

The subclass must emit a metrics counter exactly when the parent actually
compacts (returns a non-None state update) — and never when it returns None.
"""

from __future__ import annotations

import pytest
from langchain.agents.middleware import SummarizationMiddleware

import metrics
from graph.middleware.compaction import CountingSummarizationMiddleware


def _instance():
    # Skip the heavy __init__ (needs a model); we only exercise the override.
    return object.__new__(CountingSummarizationMiddleware)


def test_counts_when_parent_compacts(monkeypatch):
    calls = []
    monkeypatch.setattr(metrics, "record_compaction", lambda: calls.append(1))
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    out = _instance().before_model({"messages": []}, None)
    assert out == {"messages": []}     # parent result passed through
    assert calls == [1]                # counted once


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
