"""Per-turn user feedback routes (ADR 0126 E2 / D3) — POST/GET /api/feedback.

Stores a thumbs rating in the telemetry store's ``feedback`` table and mirrors it into
Langfuse via the bd-1cky.1 ``tracing.score_trace`` helper (faked here). Covers store +
score, replace, un-rate, incognito refusal, the no-trace_id (stored, not scored) path, and
the GET join that hands the console a session's per-turn trace_ids + current ratings.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.telemetry_store import TelemetryStore
from operator_api.feedback_routes import register_feedback_routes


def _client(monkeypatch, store, scores, *, tracing_enabled=True):
    import runtime.state as rs
    from observability import tracing

    monkeypatch.setattr(rs.STATE, "telemetry_store", store, raising=False)
    # Pin tracing so the GET's `tracing_enabled` is deterministic regardless of what a
    # sibling test or the ambient Langfuse env left on the module.
    monkeypatch.setattr(tracing, "is_enabled", lambda: tracing_enabled)

    def _fake_score(trace_id, name, value, comment=""):
        scores.append((trace_id, name, value, comment))

    # The route does `from observability import tracing; tracing.score_trace(...)`, so
    # patching the module attribute swaps in the fake for the in-function import too.
    monkeypatch.setattr(tracing, "score_trace", _fake_score)

    app = FastAPI()
    register_feedback_routes(app)
    return TestClient(app)


def _seed_turn(store, *, task_id, session_id="s1", trace_id="", created_at="2026-10-01T00:00:00+00:00"):
    """A recorded turn leg — what the GET join and the stored-rating assertions read back."""
    store.record(
        {
            "task_id": task_id,
            "session_id": session_id,
            "trace_id": trace_id,
            "state": "completed",
            "created_at": created_at,
            "ended_at": created_at,
        }
    )


@pytest.fixture
def store(tmp_path):
    return TelemetryStore(str(tmp_path / "telemetry.db"))


def _rating_for(store, trace_id, session_id="s1"):
    """The current rating on one turn, via the same join the GET serves."""
    for t in store.session_feedback(session_id):
        if (t["trace_id"] or "") == trace_id:
            return t["rating"]
    return None


def test_post_stores_row_and_scores_the_trace(monkeypatch, store):
    scores: list = []
    _seed_turn(store, task_id="t1", trace_id="tr1")
    c = _client(monkeypatch, store, scores)

    r = c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": 1, "note": "great answer"})
    assert r.status_code == 200
    body = r.json()
    assert body["rating"] == 1
    assert body["scored"] is True
    # Stored locally…
    assert _rating_for(store, "tr1") == 1
    # …and mirrored to Langfuse as a user_feedback score on the past trace.
    assert scores == [("tr1", "user_feedback", 1.0, "great answer")]


def test_a_second_rating_replaces_the_first(monkeypatch, store):
    scores: list = []
    _seed_turn(store, task_id="t1", trace_id="tr1")
    c = _client(monkeypatch, store, scores)

    assert c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": 1}).status_code == 200
    assert c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": 0}).status_code == 200

    # One turn, one feedback row — the down-vote replaced the up-vote, not stacked beside it.
    rows = [t for t in store.session_feedback("s1") if t["trace_id"] == "tr1"]
    assert len(rows) == 1
    assert rows[0]["rating"] == 0
    # Each rating was scored; the second value reached Langfuse.
    assert [s[2] for s in scores] == [1.0, 0.0]


def test_null_value_un_rates_and_deletes_the_row(monkeypatch, store):
    scores: list = []
    _seed_turn(store, task_id="t1", trace_id="tr1")
    c = _client(monkeypatch, store, scores)

    c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": 1})
    assert _rating_for(store, "tr1") == 1

    r = c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": None})
    assert r.status_code == 200
    assert r.json()["rating"] is None
    assert r.json()["removed"] is True
    # The row is gone — the turn reads as unrated again.
    assert _rating_for(store, "tr1") is None


def test_incognito_session_is_refused_and_nothing_is_stored_or_scored(monkeypatch, store):
    scores: list = []
    _seed_turn(store, task_id="t1", trace_id="tr1")
    c = _client(monkeypatch, store, scores)

    r = c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": 1, "incognito": True})
    assert r.status_code == 409
    # Nothing persisted, nothing scored.
    assert _rating_for(store, "tr1") is None
    assert scores == []


def test_missing_trace_id_is_stored_but_not_scored(monkeypatch, store):
    scores: list = []
    # A turn recorded with tracing off carries no trace_id.
    _seed_turn(store, task_id="t1", trace_id="")
    c = _client(monkeypatch, store, scores, tracing_enabled=False)

    r = c.post("/api/feedback", json={"session_id": "s1", "trace_id": "", "value": 0, "note": "meh"})
    assert r.status_code == 200
    assert r.json()["scored"] is False
    # Stored locally against the session's empty-trace key…
    assert _rating_for(store, "") == 0
    # …but score_trace was never called (no trace to score).
    assert scores == []


def test_get_returns_the_sessions_trace_ids_and_current_ratings(monkeypatch, store):
    scores: list = []
    # Three turns in one session, in order; a fourth in a different session must not leak in.
    _seed_turn(store, task_id="t1", trace_id="tr1", created_at="2026-10-01T00:00:00+00:00")
    _seed_turn(store, task_id="t2", trace_id="tr2", created_at="2026-10-01T00:01:00+00:00")
    _seed_turn(store, task_id="t3", trace_id="tr3", created_at="2026-10-01T00:02:00+00:00")
    _seed_turn(store, task_id="t9", session_id="other", trace_id="tr9")
    c = _client(monkeypatch, store, scores)

    c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr2", "value": 1, "note": "yes"})

    body = c.get("/api/feedback", params={"session_id": "s1"}).json()
    assert body["enabled"] is True
    assert body["tracing_enabled"] is True
    turns = body["turns"]
    # Oldest first, scoped to the session, carrying each turn's trace_id.
    assert [t["trace_id"] for t in turns] == ["tr1", "tr2", "tr3"]
    assert [t["task_id"] for t in turns] == ["t1", "t2", "t3"]
    # Only the rated turn carries a rating; the rest come back unrated.
    assert [t["rating"] for t in turns] == [None, 1, None]
    rated = next(t for t in turns if t["trace_id"] == "tr2")
    assert rated["note"] == "yes"


def test_get_degrades_when_the_store_is_off(monkeypatch):
    scores: list = []
    c = _client(monkeypatch, None, scores)
    body = c.get("/api/feedback", params={"session_id": "s1"}).json()
    assert body["enabled"] is False
    assert body["turns"] == []


def test_an_out_of_range_value_is_rejected(monkeypatch, store):
    scores: list = []
    c = _client(monkeypatch, store, scores)
    r = c.post("/api/feedback", json={"session_id": "s1", "trace_id": "tr1", "value": 2})
    assert r.status_code == 422
    assert scores == []


def test_a_blank_session_id_is_rejected(monkeypatch, store):
    scores: list = []
    c = _client(monkeypatch, store, scores)
    r = c.post("/api/feedback", json={"session_id": "   ", "trace_id": "tr1", "value": 1})
    assert r.status_code == 400
