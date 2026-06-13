"""Chat / goal / health / OpenAI-compat routes (ADR 0023 phase 3 extraction)."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, *, graph=object(), goal=None, chat_reply=None):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id):
        return chat_reply or [{"role": "assistant", "content": f"echo:{message}"}]

    monkeypatch.setattr(cr, "chat", _fake_chat)
    monkeypatch.setattr(cr, "agent_name", lambda: "protoagent")
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    monkeypatch.setattr(rs.STATE, "goal_controller", goal, raising=False)
    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)
    app = FastAPI()
    cr.register_chat_routes(app, ui="none")
    return TestClient(app)


def test_api_chat_joins_assistant_parts(monkeypatch):
    c = _client(monkeypatch)
    body = c.post("/api/chat", json={"message": "hi"}).json()
    assert body["response"] == "echo:hi"


def test_delete_session_harvest_is_opt_in(monkeypatch):
    # Deleting a chat must NOT silently copy it into the knowledge base: the
    # route defaults harvest=False and forwards the dialog checkbox explicitly.
    import operator_api.chat_routes as cr

    calls: list[tuple] = []

    async def _fake_retire(thread_id, *, harvest=None):
        calls.append((thread_id, harvest))
        return "chunk-1" if harvest else None

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    c = _client(monkeypatch)

    body = c.delete("/api/chat/sessions/s1").json()
    assert body == {"deleted": True, "harvested": False}
    body = c.delete("/api/chat/sessions/s2?harvest=true").json()
    assert body == {"deleted": True, "harvested": True}
    assert calls == [("a2a:s1", False), ("a2a:s2", True)]


def test_healthz_ready_and_echoes_ui(monkeypatch):
    c = _client(monkeypatch, graph=object())
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json()["graph_compiled"] is True and r.json()["ui"] == "none"


def test_healthz_503_when_graph_none(monkeypatch):
    c = _client(monkeypatch, graph=None)
    r = c.get("/healthz")
    assert r.status_code == 503 and r.json()["ok"] is False


def test_goal_disabled_when_no_controller(monkeypatch):
    c = _client(monkeypatch, goal=None)
    assert c.get("/api/goal/s1").json() == {"enabled": False, "goal": None}
    assert c.delete("/api/goal/s1").json() == {"enabled": False, "cleared": False}


def test_openai_models_and_completion(monkeypatch):
    c = _client(monkeypatch)
    models = c.get("/v1/models").json()
    assert models["data"][0]["id"] == "protoagent"
    comp = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "yo"}]}).json()
    assert comp["choices"][0]["message"]["content"] == "echo:yo"
    assert comp["model"] == "protoagent"


def test_openai_streaming(monkeypatch):
    c = _client(monkeypatch)
    r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "yo"}], "stream": True})
    assert r.headers["content-type"].startswith("text/event-stream")
    frames = [ln for ln in r.text.splitlines() if ln.startswith("data: ")]
    first = json.loads(frames[0][len("data: "):])
    assert first["choices"][0]["delta"]["content"] == "echo:yo"
    assert frames[-1] == "data: [DONE]"
