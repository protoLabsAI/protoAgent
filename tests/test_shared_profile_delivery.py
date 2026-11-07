"""Shared operator-profile delivery + writes (ADR 0125 P2-G2, #4630 slice 5b).

An opted-in agent (``memory.shared_profile``) receives the host-shared operator
profile — CONFIRMED rows from ``STATE.shared_profile_store`` — under its own
"Operator profile (shared)" section, placed before "Always-on memory":

- confirmed rows deliver; pending rows never do (they await operator confirm);
- a private live row on the same subject+kind suppresses the shared row (private wins);
- disabled (the default) delivers nothing and never opens ``profile.db``;
- ``memory_save(shared=True)`` proposes a pending row to the shared store;
- the injection-log row tags a shared id ``layer=memory``, ``source=shared_profile``.

Fakes at the store boundary; ``STATE.{shared_profile_store, memory_store}`` are the
retrieval sources the composer reads.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from graph.projection import ProjectionOptions, compose_projected_context
from knowledge.memories import MemoryStore
from knowledge.shared_profile import SharedProfileStore

_QUERY = "deployment schedule window"


@pytest.fixture(autouse=True)
def _quiet_state(monkeypatch):
    """No working-state block, no private memory, no shared store — each test wires
    exactly what it needs so the projection is just the injected-memory layer."""
    import runtime.state as rs

    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)
    monkeypatch.setattr(rs.STATE, "memory_store", None, raising=False)
    monkeypatch.setattr(rs.STATE, "shared_profile_store", None, raising=False)


def _use(monkeypatch, *, shared=None, private=None):
    import runtime.state as rs

    if shared is not None:
        monkeypatch.setattr(rs.STATE, "shared_profile_store", shared, raising=False)
    if private is not None:
        monkeypatch.setattr(rs.STATE, "memory_store", private, raising=False)


def _compose(*, opts=None, incognito=False, record=False, query=_QUERY):
    return compose_projected_context(
        query,
        None,  # no knowledge store — keep the projection to the memory layer
        None,  # no skills index
        {"session_id": "s-1"},
        incognito=incognito,
        record=record,
        options=opts or ProjectionOptions(),
        prior_sessions=lambda *a, **k: ("", []),
    )


def _profile_section(text: str) -> str:
    marker = "[Operator profile (shared):]"
    if marker not in text:
        return ""
    return text[text.index(marker) :].split("\n\n", 1)[0].split("\n</injected_memory>", 1)[0]


# ── r1 — confirmed rows deliver under their own section; pending never ────────


def test_confirmed_shared_rows_deliver_pending_do_not(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path)
    store.add("Operator is Josh in America/Chicago", kind="profile", provenance="operator", delivery="always")
    store.add(
        "deployment schedule window is 09:00 operator preference",
        kind="standing",
        provenance="operator",
        delivery="retrieved",
    )
    # A pending (agent-proposed) row must NOT reach the prompt.
    store.add("UNCONFIRMED guess about the operator", kind="profile", provenance="agent", delivery="always")
    _use(monkeypatch, shared=store)

    p = _compose(opts=ProjectionOptions(top_k=5))

    assert "[Operator profile (shared):]" in p.text
    assert "Operator is Josh in America/Chicago" in p.text  # confirmed always row
    assert "deployment schedule window is 09:00" in p.text  # confirmed retrieved row (query match)
    assert "UNCONFIRMED guess" not in p.text  # pending never delivers
    assert len(p.shared_profile_ids) == 2
    assert all(h.startswith("shared_profile:") for h in p.shared_profile_ids)


def test_section_leads_before_always_on_memory(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path / "commons")
    store.add("shared operator identity line", kind="profile", provenance="operator", delivery="always")
    private = MemoryStore(tmp_path / "agent.db")
    private.add("private always-on rule", kind="standing", provenance="operator", delivery="always")
    _use(monkeypatch, shared=store, private=private)

    p = _compose(opts=ProjectionOptions(top_k=5))

    assert "[Operator profile (shared):]" in p.text and "[Always-on memory:]" in p.text
    assert p.text.index("[Operator profile (shared):]") < p.text.index("[Always-on memory:]")


# ── r2 — a private live row on the same subject+kind suppresses the shared row ─


def test_private_row_suppresses_shared_row_same_subject_and_kind(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path / "commons")
    store.add("shared: operator is called Jo", kind="profile", provenance="operator", subject="operator-name", delivery="always")
    store.add("shared: operator timezone preference", kind="standing", provenance="operator", subject="operator-tz", delivery="always")
    store.add("shared: operator likes terse replies", kind="profile", provenance="operator", delivery="always")  # null subject

    private = MemoryStore(tmp_path / "agent.db")
    # Same subject AND kind as the first shared row → the private row wins.
    private.add("private: operator is called Joanne", kind="profile", provenance="operator", subject="operator-name")
    _use(monkeypatch, shared=store, private=private)

    p = _compose(opts=ProjectionOptions(top_k=5))

    assert "shared: operator is called Jo" not in p.text  # suppressed by the private subject+kind
    assert "shared: operator timezone preference" in p.text  # different subject → delivered
    assert "shared: operator likes terse replies" in p.text  # null subject never collides


def test_private_row_of_a_different_kind_does_not_suppress(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path / "commons")
    store.add("shared profile on the db subject", kind="profile", provenance="operator", subject="the-db", delivery="always")

    private = MemoryStore(tmp_path / "agent.db")
    # Same subject, DIFFERENT kind (standing vs profile) → no collision.
    private.add("private standing note on the db", kind="standing", provenance="operator", subject="the-db")
    _use(monkeypatch, shared=store, private=private)

    p = _compose(opts=ProjectionOptions(top_k=5))

    assert "shared profile on the db subject" in p.text


# ── r3 — disabled: nothing delivered, profile.db never opened ─────────────────


def test_disabled_delivers_nothing_from_profile_db(tmp_path, monkeypatch):
    # A populated store exists on disk, but it is NOT wired into STATE (disabled).
    orphan = SharedProfileStore(tmp_path / "commons")
    orphan.add("this must never reach a disabled agent", kind="profile", provenance="operator", delivery="always")
    _use(monkeypatch, shared=None)  # memory.shared_profile off → STATE.shared_profile_store is None

    p = _compose(opts=ProjectionOptions(top_k=5))

    assert "[Operator profile (shared):]" not in p.text
    assert "this must never reach a disabled agent" not in p.text
    assert p.shared_profile_ids == []


def test_disabled_build_returns_none_and_never_opens_profile_db(tmp_path, monkeypatch):
    from graph.config import LangGraphConfig
    from server.agent_init import _build_shared_profile_store

    monkeypatch.delenv("KNOWLEDGE_DB_PATH", raising=False)
    commons = tmp_path / "commons"
    cfg = LangGraphConfig()  # memory_shared_profile defaults False
    cfg.commons_path = str(commons)

    assert _build_shared_profile_store(cfg) is None
    assert not (commons / "profile.db").exists()  # never created / opened


# ── r4 — memory_save(shared=True): pending in the shared store; errors otherwise


def _memory_tools(*, shared_store, enabled, private=None):
    import runtime.state as rs
    from tools.memory_tools import _build_memory_tools

    rs.STATE.shared_profile_store = shared_store
    cfg = SimpleNamespace(knowledge_hot_write_confirm=False, memory_shared_profile=enabled)
    return {t.name: t for t in _build_memory_tools(None, cfg, memory_store=private)}


def test_memory_save_shared_profile_lands_pending(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path / "commons")
    monkeypatch.setattr("runtime.state.STATE.shared_profile_store", store, raising=False)
    tools = _memory_tools(shared_store=store, enabled=True, private=MemoryStore(tmp_path / "agent.db"))

    out = asyncio.run(
        tools["memory_save"].ainvoke(
            {
                "content": "Operator is based in America/Chicago",
                "kind": "profile",
                "subject": "operator-tz",
                "shared": True,
                "state": {"session_id": "a2a:chat-7"},
            }
        )
    )
    assert "shared operator profile" in out
    assert "reaches no agent until the operator confirms it" in out

    pending = store.list(review_state="pending")
    assert len(pending) == 1
    row = pending[0]
    assert row.content == "Operator is based in America/Chicago"
    assert row.kind == "profile"
    assert row.provenance == "agent"
    assert row.review_state == "pending"
    assert row.source_session == "a2a:chat-7"


def test_memory_save_shared_rejects_non_profile_kind(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path / "commons")
    monkeypatch.setattr("runtime.state.STATE.shared_profile_store", store, raising=False)
    tools = _memory_tools(shared_store=store, enabled=True)

    out = asyncio.run(
        tools["memory_save"].ainvoke({"content": "a per-session fact", "kind": "fact", "shared": True})
    )
    assert out.startswith("Error:")
    assert "profile" in out and "standing" in out
    assert store.count() == 0  # nothing written


def test_memory_save_shared_errors_when_store_off(tmp_path, monkeypatch):
    # Built enabled so the ``shared`` arg exists, but the runtime store is absent
    # (memory.shared_profile off) — the invocation-time guard returns a clear error.
    monkeypatch.setattr("runtime.state.STATE.shared_profile_store", None, raising=False)
    tools = _memory_tools(shared_store=None, enabled=True)

    out = asyncio.run(
        tools["memory_save"].ainvoke({"content": "operator identity", "kind": "profile", "shared": True})
    )
    assert out.startswith("Error:")
    assert "memory.shared_profile is disabled" in out


def test_shared_arg_hidden_unless_enabled(tmp_path):
    from tools.memory_tools import _build_memory_tools

    off_cfg = SimpleNamespace(knowledge_hot_write_confirm=False)  # no memory_shared_profile
    on_cfg = SimpleNamespace(knowledge_hot_write_confirm=False, memory_shared_profile=True)
    off = {t.name: t for t in _build_memory_tools(None, off_cfg)}
    on = {t.name: t for t in _build_memory_tools(None, on_cfg)}
    assert "shared" not in off["memory_save"].args
    assert "shared" in on["memory_save"].args


# ── r5 — injection-log rows tag a shared id layer=memory, source=shared_profile


def test_injection_log_tags_shared_rows(tmp_path, monkeypatch):
    import observability.injection_log as il
    from observability.injection_log import InjectionLog

    log = InjectionLog(str(tmp_path / "inj.db"))
    monkeypatch.setattr(il, "injection_log", lambda: log)

    store = SharedProfileStore(tmp_path / "commons")
    always_id = store.add("shared always identity", kind="profile", provenance="operator", delivery="always")
    recalled_id = store.add(
        "deployment schedule window shared preference", kind="standing", provenance="operator", delivery="retrieved"
    )
    _use(monkeypatch, shared=store)

    _compose(opts=ProjectionOptions(top_k=5), record=True)

    row = log.recent()[0]
    mem_layer = row["layers"]["memory"]
    # layer=memory: both shared ids land in the memory layer, never knowledge.
    assert f"shared_profile:{always_id}" in mem_layer
    assert f"shared_profile:{recalled_id}" in mem_layer
    assert row["layers"]["knowledge"] == []
    # source=shared_profile: the shared handle prefix marks the source distinctly from
    # a private "memory:<id>" row.
    assert all(h.startswith("shared_profile:") for h in mem_layer)
    assert f"shared_profile:{always_id}" in row["retrieved_memory_ids"]


# ── incognito suppresses the shared profile like every other memory part ──────


def test_incognito_suppresses_shared_profile(tmp_path, monkeypatch):
    store = SharedProfileStore(tmp_path / "commons")
    store.add("shared identity under incognito", kind="profile", provenance="operator", delivery="always")
    _use(monkeypatch, shared=store)

    p = _compose(opts=ProjectionOptions(top_k=5), incognito=True)

    assert "[Operator profile (shared):]" not in p.text
    assert p.shared_profile_ids == []
