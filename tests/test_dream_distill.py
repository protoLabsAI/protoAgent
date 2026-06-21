"""ADR 0054: the `dream` (memory consolidation + pruning) and `distill`
(workflow → skill packaging) curation subagents and the scoped tools they run
on (`recent_activity`, `list_skills`, `save_skill`, `forget_memory`)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from activity.store import ActivityLog
from graph.skills.index import SkillsIndex
from knowledge.store import KnowledgeStore
from observability.telemetry_store import TelemetryStore
from runtime.state import STATE
from tools.lg_tools import (
    _build_curation_tools,
    _build_memory_tools,
    get_all_tools,
    load_skill,
)


def _by_name(tools):
    return {t.name: t for t in tools}


# ── wiring / contract ─────────────────────────────────────────────────────────


def test_curation_tools_present_and_subagents_registered():
    names = {t.name for t in get_all_tools(knowledge_store=None, scheduler=None)}
    assert {"recent_activity", "list_skills", "save_skill"} <= names
    # load_skill is a lead-agent tool (the on-demand half of progressive disclosure,
    # ADR 0060) — present in the full set and always-on under deferral.
    assert "load_skill" in names
    from tools.lg_tools import DEFERRED_BASE_TOOL_NAMES

    assert "load_skill" in DEFERRED_BASE_TOOL_NAMES

    from graph.subagents.config import SUBAGENT_REGISTRY

    assert "dream" in SUBAGENT_REGISTRY and "distill" in SUBAGENT_REGISTRY

    from graph.slash_commands import resolve_slash_commands, slash_kind

    assert slash_kind("dream") == "subagent"
    assert slash_kind("distill") == "subagent"
    palette = {c["name"]: c["kind"] for c in resolve_slash_commands()}
    assert palette.get("dream") == "subagent"
    assert palette.get("distill") == "subagent"


def test_subagent_allowlists_resolve_against_full_toolset(tmp_path):
    """Every tool a dream/distill run names must exist in the full set the
    out-of-graph runner builds — otherwise it silently degrades (the class of
    bug where distill's `task_create` vanished because the runner didn't pass
    `tasks_store`)."""
    from graph.subagents.config import DISTILL_CONFIG, DREAM_CONFIG

    # Mirror run_manual_subagent's tool set with every store wired.
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))

    class _Tasks:  # builders only need a truthy object; methods are call-time.
        pass

    names = {
        t.name
        for t in get_all_tools(
            knowledge_store=ks,
            scheduler=None,
            inbox_store=None,
            tasks_store=_Tasks(),
            goal_enabled=False,
        )
    }
    for cfg in (DREAM_CONFIG, DISTILL_CONFIG):
        missing = [n for n in cfg.tools if n not in names]
        assert not missing, f"{cfg.name} names tools absent from the full set: {missing}"


# ── recent_activity ───────────────────────────────────────────────────────────


def test_recent_activity_reads_activity_and_telemetry(tmp_path, monkeypatch):
    al = ActivityLog(str(tmp_path / "a.db"))
    al.add(context_id="c", origin="scheduler", trigger="nightly", text="ran a backtest")
    al.add(context_id="c", origin="operator", text="asked about ore prices")

    ts = TelemetryStore(str(tmp_path / "t.db"))
    now = datetime.now(timezone.utc).isoformat()
    ts.record(
        {
            "task_id": "t1",
            "model": "claude-x",
            "success": 1,
            "tool_calls": 3,
            "cost_usd": 0.01,
            "created_at": now,
            "ended_at": now,
        }
    )

    monkeypatch.setattr(STATE, "activity_log", al)
    monkeypatch.setattr(STATE, "telemetry_store", ts)

    recent_activity = _by_name(_build_curation_tools())["recent_activity"]
    out = recent_activity.invoke({"limit": 10, "window_hours": 168})
    assert "ran a backtest" in out
    assert "asked about ore prices" in out
    assert "Recent activity" in out
    assert "Telemetry" in out  # the rollup rendered (1 turn recorded)


def test_recent_activity_empty(monkeypatch):
    monkeypatch.setattr(STATE, "activity_log", None)
    monkeypatch.setattr(STATE, "telemetry_store", None)
    recent_activity = _by_name(_build_curation_tools())["recent_activity"]
    out = recent_activity.invoke({})
    assert "No activity or telemetry" in out


# ── list_skills / save_skill (additive-only) ──────────────────────────────────


def test_save_skill_creates_then_refuses_duplicate(tmp_path, monkeypatch):
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    tools = _by_name(_build_curation_tools())
    save_skill, list_skills = tools["save_skill"], tools["list_skills"]

    out = save_skill.invoke(
        {
            "name": "Nightly ore run",
            "description": "Buy ore at A, sell at B when the spread clears fees",
            "body": "1. check spread\n2. buy\n3. sell",
            "tools": ["calculator"],
        }
    )
    assert "Created skill" in out

    # It landed as a curator-managed (non-disk) skill.
    skills = idx.all_skills()
    assert any(s["name"] == "Nightly ore run" and s["source"] == "distilled" for s in skills)
    assert "Nightly ore run" in list_skills.invoke({})

    # Additive-only: a second save with the same name is refused, not overwritten.
    dup = save_skill.invoke(
        {
            "name": "Nightly ore run",
            "description": "different desc",
            "body": "x",
        }
    )
    assert "already exists" in dup
    assert sum(1 for s in idx.all_skills() if s["name"] == "Nightly ore run") == 1


def test_save_skill_requires_name_and_description(tmp_path, monkeypatch):
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    save_skill = _by_name(_build_curation_tools())["save_skill"]
    assert "name is required" in save_skill.invoke({"name": "  ", "description": "d", "body": "b"})
    assert "description is required" in save_skill.invoke({"name": "n", "description": "", "body": "b"})


# ── load_skill (on-demand body lookup, ADR 0060) ──────────────────────────────


def test_load_skill_returns_full_procedure(tmp_path, monkeypatch):
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    save_skill = _by_name(_build_curation_tools())["save_skill"]
    save_skill.invoke(
        {
            "name": "Nightly ore run",
            "description": "Buy ore at A, sell at B",
            "body": "1. check spread\n2. buy\n3. sell",
            "tools": ["calculator"],
        }
    )

    out = load_skill.invoke({"name": "Nightly ore run"})
    assert "## Procedure" in out
    assert "1. check spread" in out  # the full body, loaded on demand
    assert "calculator" in out  # relevant tools surfaced


def test_load_skill_unknown_name_lists_available(tmp_path, monkeypatch):
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    _by_name(_build_curation_tools())["save_skill"].invoke(
        {"name": "Real skill", "description": "d", "body": "b"}
    )
    out = load_skill.invoke({"name": "typo-skill"})
    assert "No skill named" in out
    assert "Real skill" in out  # recovers by offering the discoverable set


def test_load_skill_unknown_name_caps_the_hint(tmp_path, monkeypatch):
    """A large library must not blow up the not-found hint — cap at 40 + "+N more"."""
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    save_skill = _by_name(_build_curation_tools())["save_skill"]
    for i in range(50):
        save_skill.invoke({"name": f"skill-{i:02d}", "description": "d", "body": "b"})
    out = load_skill.invoke({"name": "nope"})
    assert out.count("skill-") == 40  # only 40 names listed
    assert "+10 more — call list_skills" in out


def test_load_skill_no_index(monkeypatch):
    monkeypatch.setattr(STATE, "skills_index", None)
    assert "not available" in load_skill.invoke({"name": "anything"})


# ── forget_memory + memory_list id surfacing (dream's prune half) ──────────────


def test_memory_list_surfaces_id_and_forget_removes_chunk(tmp_path):
    ks = KnowledgeStore(db_path=str(tmp_path / "kb.db"))
    tools = _by_name(_build_memory_tools(ks))
    memory_ingest, memory_list, forget_memory = (tools["memory_ingest"], tools["memory_list"], tools["forget_memory"])

    asyncio.run(memory_ingest.ainvoke({"content": "ephemeral fact to prune", "domain": "general"}))
    listed = asyncio.run(memory_list.ainvoke({}))
    assert "ephemeral fact to prune" in listed
    assert listed.lstrip().startswith("#")  # id is led with for targeting

    # Pull the id out of the "#<id> ..." line.
    chunk_id = int(listed.split("#", 1)[1].split()[0])
    out = asyncio.run(forget_memory.ainvoke({"chunk_id": chunk_id, "reason": "superseded"}))
    assert f"#{chunk_id}" in out and "Forgot" in out

    after = asyncio.run(memory_list.ainvoke({}))
    assert "ephemeral fact to prune" not in after

    # Forgetting a non-existent id is a no-op, not an error.
    again = asyncio.run(forget_memory.ainvoke({"chunk_id": chunk_id}))
    assert "nothing deleted" in again
