"""ADR 0125 P2-D1 — the switch-over: memory_save/search/list/forget over the core
``memories`` store, knowledge_search over ``chunks``, the boot migration wired, and the
old tool names (memory_ingest/memory_recall/memory_stats/forget_memory) removed."""

from __future__ import annotations

import asyncio
import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge.memories import MemoryStore
from knowledge.memories_migration import migrate_memory_chunks_to_memories
from knowledge.store import KnowledgeStore
from tools.lg_tools import MEMORY_TOOL_NAMES, _build_memory_tools, get_all_tools


def _by_name(tools):
    return {t.name: t for t in tools}


def _tools(tmp_path, *, confirm=False):
    """Memory + knowledge tools bound to a fresh core agent.db (chunks + memories live
    in the SAME file, each store owning its own tables)."""
    path = str(tmp_path / "agent.db")
    knowledge = KnowledgeStore(db_path=path)
    memory = MemoryStore(db_path=path)
    cfg = SimpleNamespace(knowledge_hot_write_confirm=confirm)
    tools = _by_name(_build_memory_tools(knowledge, cfg, memory_store=memory))
    return tools, knowledge, memory


# ── r4 — the registered tool set ─────────────────────────────────────────────


def test_old_tool_names_are_gone_new_names_present(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "agent.db"))
    names = {t.name for t in get_all_tools(knowledge_store=ks)}
    for gone in ("memory_ingest", "memory_recall", "memory_stats", "forget_memory"):
        assert gone not in names, gone
    for present in (
        "memory_save",
        "memory_search",
        "memory_list",
        "memory_forget",
        "knowledge_search",
        "knowledge_ingest",
        "session_search",
        "recall_session",
    ):
        assert present in names, present
    # The MEMORY_TOOL_NAMES catalog agrees (consumed by the wizard + MCP collision check).
    assert "memory_save" in MEMORY_TOOL_NAMES and "knowledge_search" in MEMORY_TOOL_NAMES
    assert not ({"memory_ingest", "memory_recall", "memory_stats", "forget_memory"} & set(MEMORY_TOOL_NAMES))


# ── r2 — memory_save / search / list / forget over `memories` ─────────────────


def test_repo_onboard_documented_save_matches_tool_schema(tmp_path):
    skill = Path(__file__).resolve().parents[1] / "plugins/engineer/skills/repo-onboard/SKILL.md"
    calls = re.findall(r"`(memory_save\([^`]+\))`", skill.read_text(encoding="utf-8"))
    assert len(calls) == 1
    call = ast.parse(calls[0].replace("<card>", repr("Example repository card")).replace("<name>", "example"), mode="eval").body
    args = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    tools, _ks, mem = _tools(tmp_path)
    save = tools["memory_save"]
    # Check unknown kwargs explicitly: schema parsers may silently discard extras.
    assert set(args) <= set(save.args)
    save.args_schema.model_validate(args)
    assert asyncio.run(save.ainvoke(args)).startswith("Saved memory #")
    entry = mem.list(limit=1)[0]
    assert entry.content == "Example repository card"
    assert entry.subject == "repo:example"
    assert entry.heading == "example repo card"
    assert entry.kind == "fact"


def test_save_search_list_forget_round_trip(tmp_path):
    tools, _ks, _mem = _tools(tmp_path)
    out = asyncio.run(
        tools["memory_save"].ainvoke(
            {
                "content": "Operator prefers metric units",
                "kind": "profile",
                "subject": "operator-units",
                "heading": "units",
                "state": {"session_id": "a2a:chat-5"},
            }
        )
    )
    assert out.startswith("Saved memory #")
    mid = int(out.split("#")[1].split(" ")[0])

    found = asyncio.run(tools["memory_search"].ainvoke({"query": "metric units"}))
    assert "Operator prefers metric units" in found
    assert f"#{mid}" in found
    assert "src: a2a:chat-5" in found  # source_session stamped from the injected state

    listed = asyncio.run(tools["memory_list"].ainvoke({}))
    assert "Operator prefers metric units" in listed
    # memory_list ends with a per-kind total (this replaces the removed memory_stats).
    last = listed.splitlines()[-1]
    assert last.startswith("Totals —")
    assert "profile: 1" in last and "(total 1)" in last

    forgot = asyncio.run(tools["memory_forget"].ainvoke({"memory_id": mid, "reason": "test"}))
    assert forgot == f"Forgot memory #{mid}. (test)"
    assert asyncio.run(tools["memory_search"].ainvoke({"query": "metric units"})) == "No matches."


def test_memory_save_stamps_source_session_and_is_agent_pending(tmp_path):
    tools, _ks, mem = _tools(tmp_path)
    asyncio.run(tools["memory_save"].ainvoke({"content": "a durable fact", "state": {"session_id": "a2a:chat-9"}}))
    entry = mem.list(limit=1)[0]
    assert entry.source_session == "a2a:chat-9"
    assert entry.provenance == "agent" and entry.review_state == "pending"
    # Provenance is not model-settable: neither it nor the injected state is a visible arg.
    assert "provenance" not in tools["memory_save"].args
    assert "state" not in tools["memory_save"].args


def test_memory_save_without_state_has_no_bogus_source(tmp_path):
    tools, _ks, mem = _tools(tmp_path)
    out = asyncio.run(tools["memory_save"].ainvoke({"content": "a fact with no session"}))
    assert out.startswith("Saved memory #")
    assert not mem.list(limit=1)[0].source_session  # NULL, not "None"


def test_always_on_write_gated_when_confirm_on(tmp_path):
    tools, _ks, mem = _tools(tmp_path, confirm=True)
    out = asyncio.run(tools["memory_save"].ainvoke({"content": "pin me", "delivery": "always"}))
    assert out.startswith("Error: always-on writes")
    assert mem.count() == 0  # nothing written
    # The default (retrieved) delivery is unaffected by the gate.
    ok = asyncio.run(tools["memory_save"].ainvoke({"content": "ok fact", "delivery": "retrieved"}))
    assert ok.startswith("Saved memory #")


def test_always_on_write_allowed_when_confirm_off(tmp_path):
    tools, _ks, mem = _tools(tmp_path, confirm=False)
    out = asyncio.run(tools["memory_save"].ainvoke({"content": "pin me", "delivery": "always"}))
    assert out.startswith("Saved memory #")
    assert mem.list(delivery="always")


def test_expires_in_days_validation(tmp_path):
    tools, _ks, _mem = _tools(tmp_path)
    for bad in (0, -1, 4000):
        out = asyncio.run(tools["memory_save"].ainvoke({"content": "x", "expires_in_days": bad}))
        assert out.startswith("Error: expires_in_days must be"), bad
    assert asyncio.run(tools["memory_save"].ainvoke({"content": "y", "expires_in_days": 30})).startswith(
        "Saved memory #"
    )
    # The helper also rejects fractional / bool counts (for direct + SDK callers; the tool
    # schema already blocks a non-int at the boundary).
    from tools.memory_tools import _expires_at_from_days

    assert _expires_at_from_days(2.5)[1] and _expires_at_from_days(True)[1]
    assert _expires_at_from_days(30)[0] and _expires_at_from_days(None) == (None, None)


def test_bad_kind_and_delivery_rejected(tmp_path):
    tools, _ks, _mem = _tools(tmp_path)
    assert asyncio.run(tools["memory_save"].ainvoke({"content": "x", "kind": "banana"})).startswith(
        "Error: kind must be"
    )
    assert asyncio.run(tools["memory_save"].ainvoke({"content": "x", "delivery": "sometimes"})).startswith(
        "Error: delivery must be"
    )


# ── r3 — knowledge_search on chunks, memory_search on memories, no crossover ──


def test_knowledge_search_and_memory_search_do_not_cross(tmp_path):
    tools, ks, _mem = _tools(tmp_path)
    ks.add_chunk("the grenade scatter rules say roll a die", domain="necromunda")
    asyncio.run(tools["memory_save"].ainvoke({"content": "operator prefers tabs over spaces", "kind": "profile"}))

    kn = asyncio.run(tools["knowledge_search"].ainvoke({"query": "grenade"}))
    assert "grenade scatter" in kn and "[necromunda]" in kn
    assert "tabs over spaces" not in kn  # a memory never surfaces in knowledge_search

    mem = asyncio.run(tools["memory_search"].ainvoke({"query": "tabs"}))
    assert "tabs over spaces" in mem
    assert "grenade" not in mem  # a chunk never surfaces in memory_search

    # And the reverse query finds nothing in the wrong store.
    assert asyncio.run(tools["knowledge_search"].ainvoke({"query": "tabs"})) == "No matches."
    assert asyncio.run(tools["memory_search"].ainvoke({"query": "grenade"})) == "No matches."


def test_knowledge_search_collection_filter(tmp_path):
    tools, ks, _mem = _tools(tmp_path)
    ks.add_chunk("alpha content about widgets", domain="col-a")
    ks.add_chunk("beta content about widgets", domain="col-b")
    out = asyncio.run(tools["knowledge_search"].ainvoke({"query": "widgets", "collection": "col-a"}))
    assert "alpha content" in out and "beta content" not in out


# ── r1 — the boot migration (wired right after the history migration) ─────────


def test_boot_migration_moves_memory_chunks_into_memories(tmp_path):
    from server.agent_init import _migrate_memory_chunks

    path = str(tmp_path / "agent.db")
    ks = KnowledgeStore(db_path=path)
    # An agent memory write (source_type="conversation") in a freeform domain → memories.
    ks.add_chunk(
        "operator's coffee order is a flat white", domain="preferences", source_type="conversation", heading="coffee"
    )
    # An ingested document stays in chunks (knowledge), tagged with a collection.
    ks.add_chunk("chapter three of the rulebook on terrain", domain="necromunda", source_type="pdf")
    mem = MemoryStore(db_path=path)

    _migrate_memory_chunks(mem)  # what boot calls, right after the history migration

    rows = mem.list(limit=10)
    assert any("coffee order is a flat white" in r.content for r in rows)
    assert not ks.list_chunks(domain="preferences")  # the memory left chunks
    assert ks.list_chunks(domain="necromunda")  # the ingested document stayed


# ── r6 — a migrated domain='hot' row still reaches the prompt as always-on ────


def test_migrated_hot_row_reaches_prompt_as_always_on(tmp_path, monkeypatch):
    import runtime.state as rs
    from graph.projection import ProjectionOptions, compose_projected_context

    path = str(tmp_path / "agent.db")
    ks = KnowledgeStore(db_path=path)
    ks.add_chunk("deploys ship on Fridays", domain="hot", source_type="operator", heading="ops")
    mem = MemoryStore(db_path=path)

    counts = migrate_memory_chunks_to_memories(path)
    assert counts["memories"] == 1
    assert not ks.list_chunks(domain="hot")  # the hot chunk is gone from chunks …
    assert mem.list(delivery="always")  # … and is now an always-on memory

    monkeypatch.setattr(rs.STATE, "memory_store", mem, raising=False)
    for attr in ("goal_controller", "tasks_store", "watch_controller", "scheduler"):
        monkeypatch.setattr(rs.STATE, attr, None, raising=False)  # quiet the working-state block

    projected = compose_projected_context(
        "anything",
        ks,  # has no hot chunk anymore — so the always-on text can only come from the MemoryStore
        None,
        {"session_id": "s1"},
        record=False,
        options=ProjectionOptions(top_k=0, prior_sessions_policy="off"),
    )
    assert "deploys ship on Fridays" in projected.text
    assert "Always-on memory" in projected.text


# ── r5 — the SDK surface for plugins ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_sdk_memory_search_and_save(tmp_path, monkeypatch):
    import runtime.state as rs
    from graph import sdk

    mem = MemoryStore(db_path=str(tmp_path / "agent.db"))
    monkeypatch.setattr(rs.STATE, "memory_store", mem, raising=False)

    mid = await sdk.memory_save("the staging db is at db-3", kind="fact", subject="staging-db")
    assert isinstance(mid, int)
    out = await sdk.memory_search("staging db", k=3)
    assert out and out[0]["content"] == "the staging db is at db-3"
    assert out[0]["provenance"] == "agent" and out[0]["review_state"] == "pending"

    # Degrade to a no-op when no memory store is configured.
    monkeypatch.setattr(rs.STATE, "memory_store", None, raising=False)
    assert await sdk.memory_search("x") == []
    assert await sdk.memory_save("x") is None


@pytest.mark.asyncio
async def test_sdk_knowledge_search_domain_unchanged(tmp_path, monkeypatch):
    """sdk.knowledge_search still takes domain= (the collection) and hits chunks only."""
    import runtime.state as rs
    from graph import sdk

    ks = KnowledgeStore(db_path=str(tmp_path / "agent.db"))
    ks.add_chunk("a routing lesson for the fleet", domain="loop-lessons")
    monkeypatch.setattr(rs.STATE, "knowledge_store", ks, raising=False)
    out = await sdk.knowledge_search("routing lesson", k=3, domain="loop-lessons")
    assert out and "routing lesson" in out[0]["preview"]


def test_mixed_hot_sources_audit_exact_rows(tmp_path, monkeypatch):
    from graph.projection import _always_on_memory
    from operator_api.injection_routes import _resolve_injection
    from observability.injection_log import InjectionLog
    from runtime.state import STATE

    _, ks, mem = _tools(tmp_path)
    # Separate stores deliberately allocate the same numeric id.
    cid = ks.add_chunk("backend pin", domain="hot", source_type="operator")
    mid = mem.add("core pin", kind="standing", provenance="operator", delivery="always")
    assert cid == mid
    monkeypatch.setattr(STATE, "memory_store", mem)
    monkeypatch.setattr(STATE, "knowledge_store", ks)
    text, ids = _always_on_memory(ks)
    assert "backend pin" in text and "core pin" in text
    assert ids == [f"memory:{mid}", cid]
    log = InjectionLog(str(tmp_path / "injections.db"))
    log.record(hot_chunk_ids=ids)
    detail = _resolve_injection(log.recent()[0])
    assert [r["snippet"] for r in detail["memories"]] == ["core pin", "backend pin"]
    # Newest row fills the memory cap; omitted rows must never be attributed.
    large = mem.add("x" * 6000, kind="fact", provenance="operator", delivery="always")
    text, ids = _always_on_memory(ks)
    assert f"memory:{large}" in ids and f"memory:{mid}" not in ids
    assert "core pin" not in text and "backend pin" not in text
    assert len(text) == 6000
    mem.delete(large)

    class PluginBackend:
        def get_hot_memory(self):
            return "legacy plugin pin"

    assert "legacy plugin pin" in _always_on_memory(PluginBackend())[0]


def test_core_memories_inspector_crud_review(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from graph.projection import _always_on_memory
    from operator_api.memory_routes import register_memory_routes
    from runtime.state import STATE

    _, ks, mem = _tools(tmp_path)
    cid = ks.add_chunk("chunk untouched", domain="hot", source_type="operator")
    mid = mem.add("agent pin", kind="fact", provenance="agent", delivery="always")
    retrieved = mem.add("retrieved fact", kind="fact", provenance="agent")
    hidden = mem.add("forget in progress", kind="fact", provenance="agent")
    with mem._write() as db:
        db.execute("UPDATE memories SET invalidated_at = '2026-01-01' WHERE id = ?", (hidden,))
    monkeypatch.setattr(STATE, "knowledge_store", ks)
    monkeypatch.setattr(STATE, "memory_store", mem)
    app = FastAPI()
    register_memory_routes(app)
    with TestClient(app) as client:
        rows = client.get("/api/memory/hot").json()["chunks"]
        assert {c["id"] for c in rows} == {cid, f"memory:{mid}", f"memory:{retrieved}"}
        handle = f"memory:{mid}"
        assert client.put(f"/api/memory/hot/{handle}", json={"content": "edited pin"}).status_code == 200
        assert mem.get(mid).content == "edited pin"
        assert mem.search("edited")[0].id == mid
        assert not mem.search("agent")
        assert mem.get(mid).review_state == "pending"
        for state, injected in (("rejected", False), ("confirmed", True)):
            response = client.post(f"/api/memory/chunks/{handle}/review", json={"state": state})
            assert response.status_code == 200
            assert ("edited pin" in _always_on_memory(ks)[0]) is injected
            row = next(c for c in client.get("/api/memory/hot").json()["chunks"] if c["id"] == handle)
            assert row["review_state"] == state and row["injecting"] is injected
        assert client.delete(f"/api/memory/hot/{handle}").json()["deleted"]
        assert mem.get(mid) is None
        assert ks.get_chunk(cid)["content"] == "chunk untouched"


def test_agent_hot_write_event(tmp_path, monkeypatch):
    from graph.plugins.host import HOST

    events = []
    monkeypatch.setattr(HOST, "publish", lambda topic, payload: events.append((topic, payload)))
    tools, _, mem = _tools(tmp_path)
    asyncio.run(
        tools["memory_save"].ainvoke(
            {
                "content": "visible pin",
                "delivery": "always",
                "state": {"session_id": "chat-event"},
            }
        )
    )
    mid = mem.list()[0].id
    assert events == [
        (
            "memory.hot_written",
            {
                "chunk_id": f"memory:{mid}",
                "source": "chat-event",
                "source_type": "agent",
                "preview": "visible pin",
            },
        )
    ]


def test_memory_list_eval_seeds_lists_and_cleans_core_memories(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from evals import verify

    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(tmp_path / "agent.db"))
    tools, ks, mem = _tools(tmp_path)
    cases = json.loads(Path("evals/tasks.json").read_text(encoding="utf-8"))
    case = next(c for c in cases if c["id"] == "memory_list_intent")
    assert case["expected_tools"] == ["memory_list"]
    assert verify.apply_setup(case["setup"]) is None

    listed = asyncio.run(tools["memory_list"].ainvoke({}))
    for content in ("called the dentist", "merged the auth PR"):
        assert content in listed
        assert ks.search(content) == []
    assert "fact: 2" in listed.splitlines()[-1]
    assert len(mem.list()) == 2

    verify.apply_teardown(case["teardown"])
    assert mem.list() == []
    assert "No memories." in asyncio.run(tools["memory_list"].ainvoke({}))


def test_save_evals_verify_and_clean_memories(tmp_path, monkeypatch):
    import json
    from pathlib import Path
    from evals import verify
    from evals.runner import _kb_problems

    monkeypatch.setenv("KNOWLEDGE_DB_PATH", str(tmp_path / "eval.db"))
    mem = MemoryStore()
    ks = KnowledgeStore()
    cases = json.loads(Path("evals/tasks.json").read_text(encoding="utf-8"))
    selected = [c for c in cases if c["id"] in {"memory_save_intent", "memory_note_intent", "route4659_save"}]
    assert len(selected) == 3
    for case in selected:
        marker = case["verify_kb"]["find_memory_containing"]
        assert _kb_problems(case)
        cid = ks.add_chunk(marker, domain="document")
        assert _kb_problems(case)  # a matching chunk cannot satisfy memory verification
        mid = mem.add(marker, kind="fact", provenance="agent")
        assert not _kb_problems(case)
        verify.apply_teardown(case["teardown"])
        assert mem.get(mid) is None
        assert ks.get_chunk(cid) is not None


# ── P2-E4 — bundled skills + the memory-hardening dev doc use the new names ────

# The four files ADR 0125 P2-E4 moves onto the renamed memory tools: the three bundled
# SKILL.md files (whose tool references are already contract-tested by
# test_cowork_plugin / test_engineer_plugin — they resolve against the in-tree @tool
# sweep) and the memory-hardening dev doc, which no skill-surface test reaches.
_E4_FILES = (
    "plugins/cowork/skills/consolidate-memory/SKILL.md",
    "plugins/cowork/skills/daily-brief/SKILL.md",
    "plugins/engineer/skills/repo-onboard/SKILL.md",
    "docs/dev/memory-hardening-tasklist.md",
)


def test_e4_files_name_no_retired_memory_tool():
    """A retired tool name left in a skill surfaces as missing_tools; left in the dev doc
    it points a reader at a tool that no longer exists. Neither is allowed."""
    repo = Path(__file__).resolve().parents[1]
    for rel in _E4_FILES:
        text = (repo / rel).read_text(encoding="utf-8")
        for gone in ("memory_ingest", "memory_recall", "memory_stats", "forget_memory"):
            # Word boundaries so recall_session / knowledge_search never trip the sweep.
            assert not re.search(rf"\b{gone}\b", text), f"{rel} still names {gone}"


def test_memory_hardening_doc_recall_name_resolves(tmp_path):
    """The dev doc's memory_recall → memory_search rename cites a tool that is really
    registered in a default agent build — the doc is covered by no skill-surface test."""
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "docs/dev/memory-hardening-tasklist.md").read_text(encoding="utf-8")
    assert "`memory_search`" in text and "`memory_recall`" not in text
    ks = KnowledgeStore(db_path=str(tmp_path / "agent.db"))
    names = {t.name for t in get_all_tools(knowledge_store=ks)}
    assert "memory_search" in names
