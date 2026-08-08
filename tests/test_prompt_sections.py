"""Prompt segmentation (#2243 P2) — composer parts goldens, the knowledge
middleware's context/sections pair, capture threading, store round-trip +
backfill, and the route's budget-row shape."""

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from graph.middleware.prompt_capture import PromptCaptureMiddleware
from graph.middleware.prompt_cache import PromptCacheMiddleware
from graph.middleware.request_context import request_metadata_scope
from graph.prompts import build_system_prompt, build_system_prompt_parts
from observability.prompt_snapshots import PromptSnapshotStore, prompt_snapshots

# --- composer: build_system_prompt_parts ------------------------------------


def test_parts_join_is_exactly_build_system_prompt():
    # The equivalence IS the contract: labels annotate the real prompt, never a
    # reconstruction. Pin it across arg combinations.
    for kwargs in (
        {},
        {"include_subagents": False},
        {"context": "retrieved stuff"},
        {"projects": [{"name": "p", "path": "/tmp/p", "write": True}]},
    ):
        parts = build_system_prompt_parts(**kwargs)
        assert build_system_prompt(**kwargs) == "\n\n".join(t for _l, t in parts)


def test_parts_labels_and_order():
    parts = build_system_prompt_parts(projects=[{"name": "p", "path": "/tmp/p", "write": False}])
    labels = [label for label, _ in parts]
    assert labels[0] == "SOUL"
    assert "Subagents" in labels
    assert "Managed projects" in labels
    assert labels[-2:] == ["Operating model", "Guidelines"]
    assert all(text for _label, text in parts)  # no empty sections


# --- knowledge middleware: context/context_sections pair ---------------------


def test_before_model_returns_labeled_sections(tmp_path):
    from knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("deploys go out Fridays", domain="hot", heading="ops")
    km = KnowledgeMiddlewareFactory(store)
    result = km.before_model({"messages": [HumanMessage(content="anything")]}, runtime=None)
    assert result is not None
    sections = result["context_sections"]
    labels = [s["label"] for s in sections]
    assert any(label.startswith("Injected memory") for label in labels)
    # chars annotate the REAL part texts: they must sum to the joined context
    # minus the "\n\n" separators between sections.
    total = sum(s["chars"] for s in sections)
    assert total == len(result["context"]) - 2 * (len(sections) - 1)
    # The memory label carries the id-attributed count.
    mem = next(label for label in labels if label.startswith("Injected memory"))
    assert "1 memories" in mem


def KnowledgeMiddlewareFactory(store):
    from graph.middleware.knowledge import KnowledgeMiddleware

    km = KnowledgeMiddleware(knowledge_store=store)
    km._prior_sessions_cache = ""  # skip session loading
    return km


def test_before_model_none_when_nothing_to_inject(tmp_path):
    from knowledge.store import KnowledgeStore

    km = KnowledgeMiddlewareFactory(KnowledgeStore(tmp_path / "kb.db"))
    result = km.before_model({"messages": []}, runtime=None)
    # No context → no sections either (the keys always move together).
    if result is not None:
        assert set(result) >= {"context", "context_sections"}


# --- capture threading --------------------------------------------------------


class _Req:
    def __init__(self, model_name, system_message, state=None):
        self.model = SimpleNamespace(model_name=model_name)
        self.system_message = system_message
        self.state = state or {}

    def override(self, **kw):
        r = _Req(self.model.model_name, self.system_message, self.state)
        for k, v in kw.items():
            setattr(r, k, v)
        return r


def _capture_call(req, *, stable_sections=None):
    cache = PromptCacheMiddleware()
    capture = PromptCaptureMiddleware(stable_sections=stable_sections)
    response = SimpleNamespace(result=[AIMessage(content="ok")])
    cache.wrap_model_call(req, lambda r: capture.wrap_model_call(r, lambda _r: response))


def test_capture_persists_stable_and_context_sections():
    stable_sections = [{"label": "SOUL", "chars": 6}]
    state = {
        "context": "hot memory",
        "context_sections": [{"label": "Injected memory (1 memories)", "chars": 10}],
    }
    req = _Req("claude-opus-4-7", SystemMessage(content="STABLE"), state=state)
    with request_metadata_scope({"a2a.task_id": "task-sec"}):
        _capture_call(req, stable_sections=stable_sections)
    row = prompt_snapshots().calls_for_task("task-sec")[0]
    assert row["stable_sections"] == stable_sections
    assert row["context_sections"] == state["context_sections"]


def test_capture_drops_lingering_sections_when_tail_absent():
    # A later call with no context must not pair the old sections with an
    # empty tail.
    state = {"context_sections": [{"label": "stale", "chars": 5}]}
    req = _Req("protolabs/reasoning", SystemMessage(content="S"), state=state)
    with request_metadata_scope({"a2a.task_id": "task-stale"}):
        _capture_call(req)
    row = prompt_snapshots().calls_for_task("task-stale")[0]
    assert row["context_text"] == ""
    assert row["context_sections"] is None


# --- store round-trip / backfill ----------------------------------------------


def test_store_backfills_sections_on_existing_blob(tmp_path):
    s = PromptSnapshotStore(str(tmp_path / "snaps.db"))
    s.record(task_id="t1", stable_text="PROMPT")  # pre-P2 shape: no sections
    assert s.calls_for_task("t1")[0]["stable_sections"] is None
    s.record(task_id="t2", stable_text="PROMPT", stable_sections=[{"label": "SOUL", "chars": 6}])
    # Same hash → the earlier row's blob gains the sections too.
    assert s.calls_for_task("t1")[0]["stable_sections"] == [{"label": "SOUL", "chars": 6}]


def test_store_reopen_preserves_sections(tmp_path):
    s = PromptSnapshotStore(str(tmp_path / "snaps.db"))
    s.record(
        task_id="t1",
        stable_text="P",
        stable_sections=[{"label": "SOUL", "chars": 1}],
        context_sections=[{"label": "Skills index", "chars": 4}],
        context_text="tail",
    )
    again = PromptSnapshotStore(s.path)  # migration ALTERs must no-op
    row = again.calls_for_task("t1")[0]
    assert row["stable_sections"] == [{"label": "SOUL", "chars": 1}]
    assert row["context_sections"] == [{"label": "Skills index", "chars": 4}]


# --- route shape ---------------------------------------------------------------


def test_route_emits_budget_rows(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import runtime.state as rs
    from operator_api.prompt_routes import register_prompt_routes

    prompt_snapshots().record(
        task_id="t-sec",
        stable_text="S" * 40,
        context_text="tail",
        stable_sections=[{"label": "SOUL", "chars": 40}],
        context_sections=[{"label": "Working state", "chars": 4}],
    )
    monkeypatch.setattr(rs.STATE, "graph_config", SimpleNamespace(prompt_capture_enabled=True), raising=False)
    app = FastAPI()
    register_prompt_routes(app)
    body = TestClient(app).get("/api/prompts/t-sec").json()
    assert body["calls"][0]["sections"] == [
        {"label": "SOUL", "chars": 40, "approx_tokens": 10, "scope": "stable"},
        {"label": "Working state", "chars": 4, "approx_tokens": 1, "scope": "context"},
    ]


def test_route_sections_empty_for_unsegmented_rows(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import runtime.state as rs
    from operator_api.prompt_routes import register_prompt_routes

    prompt_snapshots().record(task_id="t-old", stable_text="P")
    monkeypatch.setattr(rs.STATE, "graph_config", SimpleNamespace(prompt_capture_enabled=True), raising=False)
    app = FastAPI()
    register_prompt_routes(app)
    body = TestClient(app).get("/api/prompts/t-old").json()
    assert body["calls"][0]["sections"] == []


# --- #2388 P3: speculative compose skips the injection log --------------------


def test_compose_context_record_false_skips_injection_log(tmp_path, monkeypatch):
    from knowledge.store import KnowledgeStore

    store = KnowledgeStore(tmp_path / "kb.db")
    store.add_chunk("deploys go out Fridays", domain="hot", heading="ops")
    km = KnowledgeMiddlewareFactory(store)
    recorded = []
    monkeypatch.setattr(km, "_record_injection", lambda *a, **k: recorded.append(True))

    state = {"messages": [HumanMessage(content="anything")]}
    preview = km.compose_context(state, None, record=False)
    assert preview is not None and preview["context"]  # the full dynamic layer ran
    assert recorded == []  # …but nothing claims "this entered a turn" (ADR 0069 D6)

    km.before_model(state, runtime=None)
    assert recorded == [True]  # the real path still records
