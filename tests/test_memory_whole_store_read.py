"""Whole-store selection gates and delivery, using real stores and fake models."""

import json
import threading
from dataclasses import replace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from graph.config import LangGraphConfig
from graph.config_io import config_to_dict
from graph.projection import ProjectionOptions, compose_projected_context
from knowledge.memories import MemoryStore
from knowledge.memory_select import select_memories
from observability.injection_log import InjectionLog
from runtime.state import STATE

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory.db")
    shared = MemoryStore(tmp_path / "shared.db")
    monkeypatch.setattr(STATE, "memory_store", store)
    monkeypatch.setattr(STATE, "shared_profile_store", shared)
    monkeypatch.setattr("graph.projection.working_state_block", lambda state: "")
    log = InjectionLog(tmp_path / "injections.db")
    monkeypatch.setattr("observability.injection_log.injection_log", lambda: log)
    first = store.add("weekend popular attractions", provenance="operator", kind="fact")
    second = store.add("I avoid noisy crowded places", provenance="operator", kind="fact")
    model = Mock()
    model.invoke.return_value = AIMessage(content=json.dumps([f"memory:{second}"]))
    opts = ProjectionOptions(whole_store_read=True, select_model_name="cheap", main_model_name="main",
                             select_model_factory=lambda: model)
    return store, shared, first, second, model, opts, log


def compose(opts, **kw):
    return compose_projected_context("weekend", None, None, {"session_id": "test"},
                                     options=opts, prior_sessions=lambda **kw: ("", []), **kw)


def test_off_is_byte_identical(setup):
    store, _, first, _, model, opts, _ = setup
    expected = compose(ProjectionOptions()).text
    store.list = Mock(wraps=store.list)
    store.best_similarity = Mock(side_effect=AssertionError("must not score"))
    actual = compose(replace(opts, whole_store_read=False))
    assert actual.text == expected
    assert actual.rel_memory_ids == [f"memory:{first}"]
    model.invoke.assert_not_called()
    store.list.assert_called_once_with(delivery="always")
    store.best_similarity.assert_not_called()


@pytest.mark.parametrize("gate", ["strong", "large", "same", "zero", "incognito"])
def test_no_selector(setup, monkeypatch, gate):
    store, _, first, _, model, opts, log = setup
    if gate == "strong":
        monkeypatch.setattr(store, "best_similarity", lambda *a: opts.whole_store_weak_score)
    elif gate == "large":
        opts = replace(opts, whole_store_max_rows=1)
    elif gate == "same":
        opts = replace(opts, select_model_name="main")
    elif gate == "zero":
        opts = replace(opts, top_k=0)
    result = compose(opts, incognito=gate == "incognito")
    model.invoke.assert_not_called()
    if gate not in ("incognito", "zero"):
        assert result.rel_memory_ids == [f"memory:{first}"]
        assert log.recent()[0]["memory_selection"]["selection"] == "topk"


def test_weak_selects_and_records(setup):
    _, _, first, second, model, opts, log = setup
    result = compose(opts)
    assert result.rel_memory_ids == [f"memory:{second}"]
    assert "avoid noisy" in result.text
    assert "popular attractions" not in result.text
    model.invoke.assert_called_once()
    assert log.recent()[0]["memory_selection"] == {
        "selection": "whole_store", "candidate_count": 2, "picked_ids": [f"memory:{second}"]}
    rows = json.loads(model.invoke.call_args.args[0][1].content)["memories"]
    assert {r["id"] for r in rows} == {f"memory:{first}", f"memory:{second}"}
    assert set(rows[0]) == {"id", "kind", "subject", "content"}
    assert "untrusted reference" in model.invoke.call_args.args[0][0].content


def test_live_merge_and_source_ids(setup):
    store, shared, _, _, model, opts, _ = setup
    stale = store.add("old preference", subject="music", kind="standing", provenance="operator")
    live = store.add("new preference", subject="music", kind="standing", provenance="operator")
    hidden = shared.add("shared preference", subject=" MUSIC ", kind="standing", provenance="operator", review_state="confirmed")
    visible = shared.add("quiet museums", kind="profile", provenance="operator", review_state="confirmed")
    pending = shared.add("unreviewed", kind="profile", provenance="operator", review_state="pending")
    demand = store.add("only explicit", delivery="on_demand", kind="fact", provenance="operator")
    model.invoke.return_value = AIMessage(content=json.dumps([f"shared_profile:{visible}"]))
    result = compose(replace(opts, whole_store_max_rows=5))
    rows = json.loads(model.invoke.call_args.args[0][1].content)["memories"]
    ids = {r["id"] for r in rows}
    assert f"memory:{stale}" not in ids
    assert f"memory:{live}" in ids
    assert f"shared_profile:{hidden}" not in ids
    assert f"shared_profile:{pending}" not in ids
    assert f"memory:{demand}" not in ids
    assert result.rel_memory_ids == [f"shared_profile:{visible}"]
    assert "Operator profile (shared)" in result.text


@pytest.mark.parametrize("reply", [RuntimeError("offline"), AIMessage(content="invalid JSON")])
def test_failure_falls_back(setup, reply):
    _, _, first, _, model, opts, log = setup
    if isinstance(reply, Exception):
        model.invoke.side_effect = reply
    else:
        model.invoke.return_value = reply
    result = compose(opts)
    assert result.rel_memory_ids == [f"memory:{first}"]
    assert log.recent()[0]["memory_selection"]["selection"] == "topk"


def test_empty_selection_is_not_failure(setup):
    _, _, _, _, model, opts, log = setup
    model.invoke.return_value = AIMessage(content="[]")
    assert compose(opts).text == ""
    assert log.recent()[0]["memory_selection"]["picked_ids"] == []
    assert log.recent()[0]["memory_selection"]["selection"] == "whole_store"


def test_budget_and_log_agree(setup):
    _, _, first, second, model, opts, log = setup
    model.invoke.return_value = AIMessage(content=json.dumps([f"memory:{second}", f"memory:{first}"]))
    result = compose(replace(opts, budget_chars=150))
    assert result.rel_memory_ids == [f"memory:{second}"]
    assert log.recent()[0]["memory_selection"]["picked_ids"] == result.rel_memory_ids


def test_selector_filters_unknown_duplicate_and_invalid_ids():
    model = Mock()
    model.invoke.return_value = AIMessage(content='[1, 2, 1, true, {}, "other"]')
    assert select_memories("turn", [{"id": 1, "content": "fact"}], model=model, budget=100) == [1]


def test_timeout_returns_without_waiting(monkeypatch):
    from knowledge import memory_select
    release = threading.Event()
    finished = threading.Event()
    def invoke(_):
        release.wait(2)
        finished.set()
        return AIMessage(content="[]")
    monkeypatch.setattr(memory_select, "_TIMEOUT_SECONDS", 0.01)
    try:
        assert select_memories("turn", [], model=Mock(invoke=invoke), budget=100) is None
        assert not finished.is_set()
    finally:
        release.set()
        assert finished.wait(2)


def test_store_cosine_preserves_search(tmp_path):
    store = MemoryStore(tmp_path / "mem.db", embed_fn=lambda text: [1, 0] if "match" in text else [0, 1])
    strong = store.add("match preference", kind="fact", provenance="operator")
    weak = store.add("other preference", kind="fact", provenance="operator")
    before = store.search("match")
    assert store.best_similarity("match", [strong]) == pytest.approx(1)
    assert store.best_similarity("match", [weak]) == pytest.approx(0)
    assert store.best_similarity("match", []) == -1
    assert store.search("match") == before


@pytest.mark.parametrize("select,aux,expected", [("chosen", "aux", "chosen"), ("", "aux", "aux"), ("", "", "main")])
def test_config_roundtrip_and_resolution(select, aux, expected):
    cfg = LangGraphConfig.from_dict({"model": {"name": "main"}, "routing": {"aux_model": aux}, "memory": {
        "whole_store_read": True, "whole_store_max_rows": 17, "whole_store_weak_score": 0.7, "select_model": select}})
    cfg = LangGraphConfig.from_dict(config_to_dict(cfg))
    opts = ProjectionOptions.from_config(cfg)
    assert opts.whole_store_read
    assert opts.whole_store_max_rows == 17
    assert opts.whole_store_weak_score == 0.7
    assert opts.select_model_name == expected
    assert opts.main_model_name == "main"


def test_rereads_after_model_call(setup):
    store, _, _, second, model, opts, log = setup
    def invoke(_):
        store.update(second, content="changed while selecting")
        return AIMessage(content=json.dumps([f"memory:{second}"]))
    model.invoke.side_effect = invoke
    assert compose(opts).rel_memory_ids == []
    assert log.recent()[0]["memory_selection"]["picked_ids"] == []


def test_store_failure_preserves_topk(setup, monkeypatch):
    store, _, first, _, model, opts, _ = setup
    original = store.list
    def listing(**kwargs):
        if "offset" in kwargs:
            raise RuntimeError("read failed")
        return original(**kwargs)
    monkeypatch.setattr(store, "list", listing)
    assert compose(opts).rel_memory_ids == [f"memory:{first}"]
    model.invoke.assert_not_called()


def test_paginated_candidates_and_size_boundary(setup):
    store, _, _, second, model, opts, _ = setup
    for i in range(255):
        store.add(f"extra fact {i}", kind="fact", provenance="operator")
    assert compose(replace(opts, whole_store_max_rows=257)).rel_memory_ids == [f"memory:{second}"]
    offered = json.loads(model.invoke.call_args.args[0][1].content)["memories"]
    assert len(offered) == 257
    model.reset_mock()
    compose(replace(opts, whole_store_max_rows=256))
    model.invoke.assert_not_called()


def test_whole_store_eval_uses_runtime_selection(setup):
    from evals.memory_retrieval import whole_store
    store, shared, _, second, model, opts, _ = setup
    rows = whole_store("weekend", store, opts)
    assert [r["id"] for r in rows] == [second]
    assert rows.selection["selection"] == "whole_store"
    assert STATE.shared_profile_store is shared
    model.invoke.assert_called_once()


def test_injection_log_migrates_legacy_database(tmp_path):
    import sqlite3
    path = tmp_path / "legacy.db"
    log = InjectionLog(path)
    log.record(session_id="old")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE injections DROP COLUMN memory_selection")
    log = InjectionLog(path)
    assert log.recent()[0]["memory_selection"] == {"selection": "topk", "candidate_count": 0, "picked_ids": []}
    log.record(memory_selection={"selection": "whole_store", "candidate_count": 2, "picked_ids": ["memory:1"]})
    assert log.recent()[0]["memory_selection"]["selection"] == "whole_store"


def test_qualified_main_model_stays_off():
    cfg = LangGraphConfig(model_name="claude-example", memory_select_model="anthropic-oauth:claude-example",
                          memory_whole_store_read=True)
    assert ProjectionOptions.from_config(cfg).select_model_factory is None


def test_candidate_namespace_trust_and_expiry_gates(setup):
    store, shared, _, second, model, opts, _ = setup
    excluded = [
        store.add("other namespace", kind="fact", provenance="operator", scope="private-project"),
        store.add("untrusted extraction", kind="fact", provenance="extracted"),
        store.add("expired", kind="fact", provenance="operator", expires_at="2000-01-01T00:00:00+00:00"),
        store.add("rejected", kind="fact", provenance="operator", review_state="rejected"),
    ]
    rejected_shared = shared.add("shared other namespace", kind="profile", provenance="operator", scope="other")
    compose(replace(opts, inject_namespaces=("",), inject_min_trust=3))
    offered = {r["id"] for r in json.loads(model.invoke.call_args.args[0][1].content)["memories"]}
    assert f"memory:{second}" in offered
    assert not offered.intersection(f"memory:{i}" for i in excluded)
    assert f"shared_profile:{rejected_shared}" not in offered
