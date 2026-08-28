"""Prompt snapshot routes (#2243) — the {enabled:false} contract, 404 on an
unknown task, the wire shape, and /last ordering."""

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from observability.prompt_snapshots import prompt_snapshots
from operator_api.prompt_routes import register_prompt_routes


def _client(monkeypatch, *, capture=True, caps=None):
    import runtime.state as rs

    # `caps` = (retention_days, max_calls) on the live config, the source of
    # truth for the #3019 retention report. Omitted → a config that carries
    # neither key, which is the pre-#3019 shape the routes still tolerate.
    cfg = SimpleNamespace(prompt_capture_enabled=capture)
    if caps is not None:
        cfg.prompt_capture_retention_days, cfg.prompt_capture_max_calls = caps
    monkeypatch.setattr(rs.STATE, "graph_config", cfg, raising=False)
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
    empty = c.get("/api/prompts/last?session_id=empty").json()
    assert empty["enabled"] is True and empty["call"] is None


def test_last_reports_which_retention_cap_is_binding(monkeypatch):
    """#3019: the effective window is readable off the existing /last payload —
    an operator should not have to open prompt-snapshots.db to learn that a
    generous retention_days is being overruled by the row cap."""
    store = prompt_snapshots()
    store.retention_days, store.max_calls = 30, 2
    for i in range(4):
        store.record(task_id=f"t{i}", session_id="s1", stable_text="P")
    c = _client(monkeypatch)
    retention = c.get("/api/prompts/last?session_id=s1").json()["retention"]
    assert retention["retention_days"] == 30
    assert retention["max_calls"] == 2
    assert retention["calls"] == 2  # the row cap already threw the rest away
    assert retention["binding_cap"] == "max_calls"
    assert retention["effective_days"] < 30
    # Capture off keeps the {enabled:false} contract — no retention block to report.
    off = _client(monkeypatch, capture=False)
    assert off.get("/api/prompts/last").json() == {"enabled": False, "call": None}


def test_last_reports_the_configured_caps_not_the_stores_own(monkeypatch):
    """The caps living on the store object are the WRITER's view — the capture
    middleware stamps them on each model call — so a process that has not
    captured yet holds whatever it was constructed with. The report has to
    follow config instead (#3019), or it answers the one question it exists for
    with a stale number.

    The scenario that makes this concrete: an operator reads `binding_cap:
    "max_calls"`, raises the row cap, restarts, and opens `/prompt` on an old
    session. No capture has happened in the new process; the rows in the DB are
    still the ones the OLD cap trimmed to. Reporting the store's own caps would
    tell them the row cap is still 2 and still binding — the fix they just made,
    reported as unmade."""
    store = prompt_snapshots()
    store.retention_days, store.max_calls = 30, 2  # the pre-restart policy
    for i in range(4):
        store.record(task_id=f"t{i}", session_id="s1", stable_text="P")
    c = _client(monkeypatch, caps=(90, 40000))  # …what the operator configured since
    retention = c.get("/api/prompts/last?session_id=s1").json()["retention"]
    assert (retention["retention_days"], retention["max_calls"]) == (90, 40000)
    assert retention["calls"] == 2  # the rows the old cap left behind, reported honestly
    # 2 rows against a 40,000-row cap is headroom and the rows are minutes old
    # against a 90-day one: nothing is evicting, so the alarm clears to "none"
    # rather than echoing the old state.
    assert retention["binding_cap"] == "none"


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
    assert call["speculative"] is True
    assert call["system"]["stable"] == "STABLE-A\n\nSTABLE-B"
    assert call["system"]["context"] == ""  # legacy context channel is empty post-#3188
    assert call["projected_context"] == "TAIL"
    labels = [s["label"] for s in call["sections"]]
    assert labels == ["SOUL", "Guidelines", "Skills index"]
    assert call["usage"]["input_tokens"] == 0  # nothing ran


def _preview_with(monkeypatch, composed: dict) -> dict:
    import runtime.state as rs

    class _KM:
        def compose_context(self, state, runtime=None, *, record=True):
            return composed

    graph = SimpleNamespace(
        system_prompt_parts=[("SOUL", "STABLE-A")],
        knowledge_middleware=_KM(),
        aget_state=None,
    )
    c = _client(monkeypatch)
    monkeypatch.setattr(rs.STATE, "graph", graph, raising=False)
    return c.get("/api/prompts/preview").json()["call"]


def test_preview_carries_the_delivery_budget_and_truncated_flags(monkeypatch):
    """ADR 0108 D6: the budget in force and what was shed reach the preview API,
    and a shed section keeps its ``truncated`` flag through ``_sections``."""
    budget = {"chars": 100, "used": 7, "overflow": [{"label": "RAG hits", "dropped_items": 3, "dropped_chars": 300}]}
    call = _preview_with(
        monkeypatch,
        {
            "context": "TAIL",
            "context_sections": [
                {"label": "Injected memory (2 docs)", "chars": 4, "truncated": True},
                {"label": "Skills index", "chars": 3},
            ],
            "budget": budget,
        },
    )
    assert call["budget"] == budget
    by_label = {s["label"]: s for s in call["sections"]}
    assert by_label["Injected memory (2 docs)"]["truncated"] is True
    assert "truncated" not in by_label["Skills index"]
    assert "truncated" not in by_label["SOUL"]


def test_preview_budget_is_null_when_delivery_is_unbounded(monkeypatch):
    call = _preview_with(monkeypatch, {"context": "TAIL", "context_sections": [{"label": "Skills index", "chars": 4}]})
    assert call["budget"] is None


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
