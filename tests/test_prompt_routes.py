"""Prompt snapshot routes (#2243) — the {enabled:false} contract, 404 on an
unknown task, the wire shape, and /last ordering."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.prompt_snapshots import prompt_snapshots
from operator_api.prompt_routes import register_prompt_routes


def _client(monkeypatch, *, capture=True):
    import runtime.state as rs

    monkeypatch.setattr(rs.STATE, "graph_config", SimpleNamespace(prompt_capture_enabled=capture), raising=False)
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
    assert first["system"] == {"stable": "STABLE", "context": "tail-0", "wire_differs": False, "wire": ""}
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


# ── #2388 P3: subagents + prev on the task route; the preview route ───────────


def test_task_detail_carries_subagents_and_prev(monkeypatch):
    c = _client(monkeypatch)
    store = prompt_snapshots()
    store.record(task_id="turn-1", session_id="s-p3", stable_text="T1")
    store.record(task_id="turn-2", session_id="s-p3", stable_text="T2")
    store.record(parent_task_id="turn-2", subagent_type="researcher", stable_text="SUB")
    body = c.get("/api/prompts/turn-2").json()
    assert body["enabled"] is True and len(body["calls"]) == 1
    assert [s["subagent_type"] for s in body["subagents"]] == ["researcher"]
    # prev = the previous turn's last main call; subagent rows never anchor it.
    assert body["prev"]["system"]["stable"] == "T1"
    # The first turn has no anchor — null, never an error (#2388 degrade rule).
    assert c.get("/api/prompts/turn-1").json()["prev"] is None


def test_preview_runs_compose_without_recording(monkeypatch):
    import runtime.state as rs

    calls = {}

    class _KM:
        def compose_context(self, state, runtime=None, *, record=True):
            calls["record"] = record
            return {"context": "TAIL", "context_sections": [{"label": "Skills index", "chars": 4}]}

    graph = SimpleNamespace(
        system_prompt_parts=[("SOUL", "STABLE-A"), ("Guidelines", "STABLE-B")],
        knowledge_middleware=_KM(),
        aget_state=None,
    )
    c = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    body = c.get("/api/prompts/preview").json()
    assert calls["record"] is False  # speculation must NOT write the injection log
    call = body["call"]
    assert call["preview"] is True
    assert call["system"]["stable"] == "STABLE-A\n\nSTABLE-B"
    assert call["system"]["context"] == "TAIL"
    labels = [s["label"] for s in call["sections"]]
    assert labels == ["SOUL", "Guidelines", "Skills index"]
    assert call["usage"]["input_tokens"] == 0  # nothing ran


def test_preview_degrades_without_graph_stamps(monkeypatch):
    import runtime.state as rs

    c = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "graph", SimpleNamespace(), raising=False)
    body = c.get("/api/prompts/preview").json()
    assert body["enabled"] is True and body["call"] is None and body["reason"]
    c2 = _client(monkeypatch, capture=False)
    assert c2.get("/api/prompts/preview").json()["enabled"] is False


def test_breakdown_sizes_history_and_ignores_capture_flag(monkeypatch):
    """/breakdown reads the checkpoint, not the snapshot store — it must answer
    even with prompts.capture OFF, and count tool args exactly once (the
    graph.message_blocks contract; content also carries the tool_use blocks)."""
    import runtime.state as rs
    from langchain_core.messages import AIMessage, ToolMessage

    big = {"path": "tools.py", "content": "x" * 4000}
    msgs = [
        AIMessage(
            content=[
                {"type": "text", "text": "writing"},
                {"type": "tool_use", "id": "tu1", "name": "plugin_write_file", "input": big},
            ],
            tool_calls=[{"id": "tu1", "name": "plugin_write_file", "args": big, "type": "tool_call"}],
        ),
        ToolMessage(content="✓ wrote tools.py", tool_call_id="tu1", name="plugin_write_file"),
    ]

    class _Snap:
        values = {"messages": msgs}

    async def aget_state(cfg):
        assert "thread_id" in cfg["configurable"]
        return _Snap()

    c = _client(monkeypatch, capture=False)  # capture OFF on purpose
    monkeypatch.setattr(rs.STATE, "graph", SimpleNamespace(aget_state=aget_state), raising=False)
    body = c.get("/api/prompts/breakdown", params={"session_id": "chat-x"}).json()
    assert body["found"] is True
    b = body["breakdown"]
    # capture is OFF: sizes are metadata and stay, but the content-bearing
    # previews are redacted — the locked contract, scoped to the one such field.
    assert b["previews_redacted"] is True
    assert all(blk["preview"] == "" for blk in b["top_blocks"])
    args_tok = b["tool_call_args"]["plugin_write_file"]
    assert args_tok >= 900
    assert b["total_est_tokens"] < args_tok * 1.2  # counted once, not mirrored
    assert b["tool_results"]["plugin_write_file"]["calls"] == 1


def test_breakdown_degrades_honestly(monkeypatch):
    import runtime.state as rs

    c = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "graph", None, raising=False)
    assert c.get("/api/prompts/breakdown", params={"session_id": "s"}).json() == {
        "found": False,
        "reason": "no live agent",
    }
    assert c.get("/api/prompts/breakdown").json()["reason"] == "session_id required"


def test_shape_carries_the_owning_task_id(monkeypatch):
    """/last exposes the call's task_id so /prompt can open the SAME full dialog
    the message action opens ("" on /preview's synthesized row — no turn owns it)."""
    store = prompt_snapshots()
    store.record(task_id="task-9", session_id="sess-9", model="m", stable_text="S", context_text="C")
    c = _client(monkeypatch)
    body = c.get("/api/prompts/last", params={"session_id": "sess-9"}).json()
    assert body["call"]["task_id"] == "task-9"
