"""Prompt snapshot routes (#2243) — the {enabled:false} contract, 404 on an
unknown task, the wire shape, and /last ordering."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.prompt_snapshots import prompt_snapshots
from operator_api.prompt_routes import register_prompt_routes


def _client(monkeypatch, *, capture=True):
    import runtime.state as rs

    monkeypatch.setattr(
        rs.STATE, "graph_config", SimpleNamespace(prompt_capture_enabled=capture), raising=False
    )
    app = FastAPI()
    register_prompt_routes(app)
    return TestClient(app)


def test_routes_flip_to_disabled_when_capture_off(monkeypatch):
    c = _client(monkeypatch, capture=False)
    assert c.get("/api/prompts/last").json() == {"enabled": False, "call": None}
    assert c.get("/api/prompts/any-task").json() == {"enabled": False, "calls": []}


def test_task_detail_shape_and_call_order(monkeypatch):
    prompt_snapshots().record(
        task_id="t1",
        session_id="s1",
        stable_text="STABLE",
        context_text="tail-0",
        model="claude-opus-4-7",
        input_tokens=10,
        output_tokens=2,
        cache_read_tokens=8,
        cache_creation_tokens=1,
    )
    prompt_snapshots().record(task_id="t1", session_id="s1", stable_text="STABLE", context_text="tail-1")
    c = _client(monkeypatch)
    body = c.get("/api/prompts/t1").json()
    assert body["enabled"] is True
    assert [call["call_index"] for call in body["calls"]] == [0, 1]
    first = body["calls"][0]
    assert first["system"] == {"stable": "STABLE", "context": "tail-0"}
    assert first["model"] == "claude-opus-4-7"
    assert first["usage"] == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_tokens": 8,
        "cache_creation_tokens": 1,
    }
    assert first["ts"]


def test_unknown_task_404s(monkeypatch):
    c = _client(monkeypatch)
    r = c.get("/api/prompts/never-captured")
    assert r.status_code == 404
    assert "no prompt snapshots" in r.json()["detail"]


def test_last_returns_sessions_newest_call(monkeypatch):
    prompt_snapshots().record(task_id="t1", session_id="s1", stable_text="P", context_text="older")
    prompt_snapshots().record(task_id="t2", session_id="s1", stable_text="P", context_text="newer")
    prompt_snapshots().record(task_id="t3", session_id="s2", stable_text="P", context_text="other-session")
    c = _client(monkeypatch)
    body = c.get("/api/prompts/last?session_id=s1").json()
    assert body["enabled"] is True
    assert body["call"]["system"]["context"] == "newer"
    # No captures yet for an unknown session → null call, not a 404 (the
    # /prompt note renders "nothing captured yet").
    assert c.get("/api/prompts/last?session_id=empty").json() == {"enabled": True, "call": None}
