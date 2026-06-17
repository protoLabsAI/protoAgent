"""Chat / goal / health / OpenAI-compat routes (ADR 0023 phase 3 extraction)."""

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _client(monkeypatch, *, graph=object(), goal=None, chat_reply=None):
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_chat(message, session_id, *, model=None):
        suffix = f"@{model}" if model else ""
        return chat_reply or [{"role": "assistant", "content": f"echo:{message}{suffix}"}]

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


def test_api_chat_threads_per_tab_model(monkeypatch):
    c = _client(monkeypatch)
    body = c.post("/api/chat", json={"message": "hi", "model": "protolabs/fast"}).json()
    assert body["response"] == "echo:hi@protolabs/fast"  # model reached chat()


def test_openai_completion_honors_model_override(monkeypatch):
    c = _client(monkeypatch)
    # A real (non-agent) model id is forwarded as a per-request override.
    comp = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "yo"}], "model": "protolabs/fast"},
    ).json()
    assert comp["choices"][0]["message"]["content"] == "echo:yo@protolabs/fast"
    # The agent's own advertised id means "use the configured default" (no override).
    comp2 = c.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "yo"}], "model": "protoagent"},
    ).json()
    assert comp2["choices"][0]["message"]["content"] == "echo:yo"


def test_delete_session_harvest_is_opt_in(monkeypatch):
    # Deleting a chat must NOT silently copy it into the knowledge base: the
    # route defaults harvest=False and forwards the dialog checkbox explicitly.
    # Both a2a: and chat: prefixes are retired; only a2a: is harvested.
    import operator_api.chat_routes as cr

    calls: list[tuple] = []

    async def _fake_retire(thread_id, *, harvest=None, cascade=True):
        calls.append((thread_id, harvest, cascade))
        return "chunk-1" if harvest else None

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    c = _client(monkeypatch)

    body = c.delete("/api/chat/sessions/s1").json()
    assert body == {"deleted": True, "harvested": False}
    body = c.delete("/api/chat/sessions/s2?harvest=true").json()
    assert body == {"deleted": True, "harvested": True}
    assert calls == [
        ("a2a:s1", False, True),
        ("chat:s1", False, True),
        ("a2a:s2", True, True),
        ("chat:s2", False, True),
    ]


def test_delete_session_cleans_ephemeral_attachments(monkeypatch):
    """Deleting a chat drops its session-scoped attachment chunks."""
    import operator_api.chat_routes as cr
    import runtime.state as rs

    async def _fake_retire(thread_id, *, harvest=None, cascade=True):
        return None

    ns: list[str] = []

    class _Store:
        def delete_by_namespace(self, namespace):
            ns.append(namespace)
            return 3

    monkeypatch.setattr(cr, "_retire_thread", _fake_retire)
    c = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "knowledge_store", _Store(), raising=False)
    assert c.delete("/api/chat/sessions/s1").json()["deleted"] is True
    assert ns == ["attach:s1"]


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


def test_steer_enqueue_then_cancel_roundtrip(monkeypatch):
    # Full HTTP lifecycle of the steer endpoints: POST queues, GET peeks, DELETE
    # cancels a still-queued message, and a second DELETE reports too-late.
    from graph import steering

    steering._QUEUES.clear()
    c = _client(monkeypatch)
    posted = c.post("/api/chat/sessions/s1/steer", json={"id": "m1", "text": "do X instead"}).json()
    assert posted == {"ok": True, "id": "m1", "pending": 1}
    assert c.get("/api/chat/sessions/s1/steer").json() == {"pending": [{"id": "m1", "text": "do X instead"}]}

    # ✕ before the turn folds it in → removed, queue empties.
    assert c.delete("/api/chat/sessions/s1/steer/m1").json() == {"removed": True, "pending": 0}
    # ✕ again (or after it's drained) → too late, nothing removed.
    assert c.delete("/api/chat/sessions/s1/steer/m1").json() == {"removed": False, "pending": 0}
    steering._QUEUES.clear()


def test_delegation_list_and_cancel_roundtrip(monkeypatch):
    # HTTP lifecycle of the Tier 2 delegation routes: GET lists in-flight `task`
    # delegations; POST cancels one (the route hits graph.delegations directly).
    from graph import delegations

    class _Fake:
        def __init__(self):
            self.cancelled = False

        def done(self):
            return self.cancelled

        def cancel(self):
            self.cancelled = True

    delegations._RUNNING.clear()
    c = _client(monkeypatch)
    assert c.get("/api/chat/sessions/s1/delegations").json() == {"running": []}

    f = _Fake()
    delegations.register("s1", "d1", f, label="research X")
    assert c.get("/api/chat/sessions/s1/delegations").json() == {
        "running": [{"id": "d1", "label": "research X"}]
    }
    # Cancel the live delegation → cancelled; still counted (the tool's finally
    # unregisters once the task actually unwinds, not the route).
    assert c.post("/api/chat/sessions/s1/delegations/d1/cancel").json() == {"cancelled": True, "running": 1}
    assert f.cancelled is True
    # Cancel again (already cancelling) and an unknown id → both too-late/false.
    assert c.post("/api/chat/sessions/s1/delegations/d1/cancel").json() == {"cancelled": False, "running": 1}
    assert c.post("/api/chat/sessions/s1/delegations/nope/cancel").json() == {"cancelled": False, "running": 1}
    delegations._RUNNING.clear()


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
    first = json.loads(frames[0][len("data: ") :])
    assert first["choices"][0]["delta"]["content"] == "echo:yo"
    assert frames[-1] == "data: [DONE]"
