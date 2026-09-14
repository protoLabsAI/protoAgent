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


# ── incognito (ADR 0069 D3b, #3493) ───────────────────────────────────────────


def test_incognito_turn_is_never_archived_but_still_compacts(monkeypatch):
    """An incognito turn leaves no memory trail — the harvest already skips such a
    thread, and auto-compaction must too. The compaction itself still happens: it is
    the safety valve between the model and an overflow, and an unarchived rewrite is
    exactly the "no trail" the operator asked for."""
    from langchain_core.messages import HumanMessage

    docs = []
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    monkeypatch.setattr("knowledge.add_document", lambda store, text, **kw: docs.append(kw) or [1])
    mw = _instance()
    mw._knowledge_store = _kb()
    state = {"messages": [HumanMessage(content="my secret is teal")], "session_id": "s", "incognito": True}
    assert mw.before_model(state, None) == {"messages": []}  # still compacted
    assert docs == []  # nothing archived


@pytest.mark.asyncio
async def test_async_incognito_turn_is_never_archived(monkeypatch):
    from langchain_core.messages import HumanMessage

    docs = []

    async def _fake(self, s, r):
        return {"messages": []}

    monkeypatch.setattr(SummarizationMiddleware, "abefore_model", _fake)
    monkeypatch.setattr("knowledge.add_document", lambda store, text, **kw: docs.append(kw) or [1])
    mw = _instance()
    mw._knowledge_store = _kb()
    state = {"messages": [HumanMessage(content="my secret is teal")], "session_id": "s", "incognito": True}
    assert await mw.abefore_model(state, None) == {"messages": []}
    assert docs == []


# ── provenance + dates on the archive row (#3493) ─────────────────────────────


def _trajectory(monkeypatch, tmp_path, session, events):
    """Point the trajectory at ``tmp_path`` and seed ``request`` events: the log is
    the only record of when a message was first sent to the model."""
    import json

    from observability import trajectory as traj

    log = traj.TrajectoryLog(tmp_path)
    monkeypatch.setattr(traj, "trajectory_log", log)
    log.path_for(session).write_text(
        "".join(json.dumps({"ts": ts, "t": "request", "msgs": [{"id": i} for i in ids]}) + "\n" for ts, ids in events),
        encoding="utf-8",
    )


def test_archive_row_carries_thread_source_and_message_dates(monkeypatch, tmp_path):
    """Regression (#3493): a session compacted on 09-13 archived transcripts from
    08-31..09-09 stamped only with the compaction day and no source, and recall served
    them as current. The row now names its thread, and its text says when each line was
    written (recall shows a chunk's text and stored date — never its heading)."""
    from datetime import UTC, datetime

    import langgraph.config
    from langchain_core.messages import AIMessage, HumanMessage

    today = datetime.now(UTC).date().isoformat()
    _trajectory(
        monkeypatch,
        tmp_path,
        "sessX",
        [("2026-08-31T09:00:00+00:00", ["h1"]), ("2026-09-09T09:00:00+00:00", ["h1", "a1", "h2"])],
    )
    monkeypatch.setattr(langgraph.config, "get_config", lambda: {"configurable": {"thread_id": "a2a:sessX"}})
    docs = []
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    monkeypatch.setattr("knowledge.add_document", lambda store, text, **kw: docs.append({"text": text, **kw}) or [1])
    mw = _instance()
    mw._knowledge_store = _kb()
    state = {
        "session_id": "sessX",
        "messages": [
            HumanMessage(id="h1", content="the sky is teal"),
            AIMessage(id="a1", content="noted"),
            HumanMessage(id="h2", content="it went green"),
            HumanMessage(id="h3", content="what colour is it?"),  # this turn: not sent to the model yet
        ],
    }
    mw.before_model(state, None)
    (doc,) = docs
    assert doc["source"] == "a2a:sessX"
    assert doc["namespace"] == "chat-archive:sessX"
    assert doc["heading"] == f"Conversation archive (auto-compaction, sessX, messages 2026-08-31 to {today})"
    lines = doc["text"].splitlines()
    assert lines[0] == f"[Conversation archive: messages from 2026-08-31 to {today}; archived {today}]"
    assert lines[1:] == [
        "User [2026-08-31]: the sky is teal",
        "Assistant [2026-09-09]: noted",
        "User [2026-09-09]: it went green",
        f"User [{today}]: what colour is it?",
    ]


def test_archive_without_a_trajectory_is_marked_undated_not_new(monkeypatch, tmp_path):
    """No record of when the messages were sent: say so, rather than dating the
    whole transcript to the compaction day (the mistake this fixes)."""
    from datetime import UTC, datetime

    from langchain_core.messages import HumanMessage

    today = datetime.now(UTC).date().isoformat()
    _trajectory(monkeypatch, tmp_path, "sessY", [])
    docs = []
    monkeypatch.setattr(SummarizationMiddleware, "before_model", lambda self, s, r: {"messages": []})
    monkeypatch.setattr("knowledge.add_document", lambda store, text, **kw: docs.append({"text": text, **kw}) or [1])
    mw = _instance()
    mw._knowledge_store = _kb()
    mw.before_model({"session_id": "sessY", "messages": [HumanMessage(id="h1", content="old news")]}, None)
    (doc,) = docs
    assert doc["text"].splitlines() == [f"[Conversation archive: message dates unknown; archived {today}]", "User: old news"]
    assert doc["heading"] == f"Conversation archive (auto-compaction, sessY, archived {today})"
    assert doc["source"] is None  # no graph run → no thread to name
