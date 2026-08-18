"""Trajectory read routes (ADR 0102 D4/S2, #2806) — events paging + the
availability-joined call reconstruction."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from observability.trajectory import TrajectoryLog, message_ref
from operator_api.trajectory_routes import register_trajectory_routes


def _client(monkeypatch, tmp_path, thread_messages=None):
    tl = TrajectoryLog(tmp_path)
    monkeypatch.setattr("observability.trajectory.trajectory_log", tl)

    class _Graph:
        async def aget_state(self, config):
            return SimpleNamespace(values={"messages": list(thread_messages or [])})

    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph", _Graph() if thread_messages is not None else None, raising=False)
    app = FastAPI()
    register_trajectory_routes(app)
    return TestClient(app), tl


def test_no_log_reports_not_found(monkeypatch, tmp_path):
    c, _ = _client(monkeypatch, tmp_path)
    assert c.get("/api/trajectory/nope").json() == {"found": False, "events": [], "total": 0}


def test_events_page_from_the_tail_with_stable_indices(monkeypatch, tmp_path):
    c, tl = _client(monkeypatch, tmp_path)
    for i in range(10):
        tl.append("s1", {"t": "request", "k": i})
    body = c.get("/api/trajectory/s1?limit=3").json()
    assert body["total"] == 10
    assert [e["k"] for e in body["events"]] == [7, 8, 9]
    prev = c.get("/api/trajectory/s1?limit=3&before=7").json()
    assert [e["k"] for e in prev["events"]] == [4, 5, 6]  # absolute indices — stable paging


def test_call_reconstruction_joins_availability_against_the_checkpoint(monkeypatch, tmp_path):
    """The join the S1 refs exist for: hash-identical → available (+preview);
    same id/new bytes → rewritten; gone → missing — with the log still PROVING
    the missing message's size and position."""
    live = [
        HumanMessage(content="the question", id="h1"),
        AIMessage(content="…[pruned stub]…", id="a1"),  # body replaced in place
    ]
    c, tl = _client(monkeypatch, tmp_path, thread_messages=live)
    original = [
        HumanMessage(content="the question", id="h1"),
        AIMessage(content="a huge original answer " * 50, id="a1"),
        HumanMessage(content="compacted away later", id="h2"),
    ]
    tl.append("s1", {"t": "request", "model": "m", "stable_sha": "x", "tools_sha": "y",
                     "tools_count": 3, "msgs": [message_ref(m) for m in original]})

    body = c.get("/api/trajectory/s1/call/0").json()
    assert body["found"] is True and body["calls"] == 1
    by_id = {m["id"]: m for m in body["messages"]}
    assert by_id["h1"]["status"] == "available" and by_id["h1"]["preview"] == "the question"
    assert by_id["a1"]["status"] == "rewritten" and "pruned stub" in by_id["a1"]["preview"]
    assert by_id["h2"]["status"] == "missing" and by_id["h2"]["chars"] > 0  # proven, not present
    assert body["availability"] == {"available": 1, "rewritten": 1, "missing": 1}


def test_call_indexing_negative_and_out_of_range(monkeypatch, tmp_path):
    c, tl = _client(monkeypatch, tmp_path, thread_messages=[])
    tl.append("s1", {"t": "request", "msgs": []})
    tl.append("s1", {"t": "response", "status": "ok"})
    tl.append("s1", {"t": "request", "msgs": []})
    latest = c.get("/api/trajectory/s1/call/-1").json()
    assert latest["found"] is True and latest["call"] == 1 and latest["calls"] == 2
    assert c.get("/api/trajectory/s1/call/5").json() == {"found": False, "reason": "out_of_range", "calls": 2}
