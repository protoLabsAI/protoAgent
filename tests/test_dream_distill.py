"""ADR 0054: the `dream` (memory consolidation + pruning) and `distill`
(workflow → skill packaging) curation subagents and the scoped tools they run
on (`recent_activity`, `list_skills`, `save_skill`, `forget_memory`)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from activity.store import ActivityLog
from graph.extensions.skills import SkillV1Artifact
from graph.skills.authoring import remove_skill
from graph.skills.index import SkillsIndex
from knowledge.store import KnowledgeStore
from observability.telemetry_store import TelemetryStore
from runtime.state import STATE
from tools.lg_tools import (
    _build_curation_tools,
    _build_memory_tools,
    _build_skill_editor_tools,
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


def test_save_skill_rejects_empty_and_colliding_storage_slugs(tmp_path, monkeypatch):
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    save_skill = _by_name(_build_curation_tools())["save_skill"]

    assert "ASCII letter or digit" in save_skill.invoke({"name": "💥", "description": "d", "body": "b"})
    assert "Created skill" in save_skill.invoke({"name": "A/B", "description": "d", "body": "b"})
    assert "collides" in save_skill.invoke({"name": "A B", "description": "d", "body": "b"})


def test_skill_delete_refuses_root_and_slug_collisions(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    marker = root / "keep.txt"
    marker.write_text("keep")
    assert remove_skill(root, "💥") is False
    assert marker.exists()

    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    for name in ("A/B", "A B"):
        idx.add_skill(SkillV1Artifact(name=name, description="d", prompt_template="b"), source="distilled")
    delete_skill = _by_name(_build_skill_editor_tools())["delete_skill"]
    assert "collides" in delete_skill.invoke({"name": "A/B", "reason": "obsolete"})
    assert len(idx.all_skills()) == 2


def test_reviewer_skill_provenance_uses_trusted_injected_session(tmp_path, monkeypatch):
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    save_skill = _by_name(_build_curation_tools(provenance_required=True))["save_skill"]

    refused = save_skill.func(
        "Deploy", "d", "b", provenance_reason="", source_session_id="forged", state={"session_id": "trusted"}
    )
    assert "provenance reason" in refused
    created = save_skill.func(
        "Deploy",
        "d",
        "b",
        provenance_reason="verified retry ordering",
        source_session_id="forged",
        state={"session_id": "trusted"},
    )
    assert "Created skill" in created
    assert idx.all_skills()[0]["source_session_id"] == "trusted"


def test_reviewer_skill_writes_never_report_unconfirmed_index_mutations(tmp_path, monkeypatch):
    home = tmp_path / "instance"
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)

    save_skill = _by_name(_build_curation_tools())["save_skill"]
    save_skill.invoke({"name": "Deploy", "description": "old", "body": "old body"})
    original_delete = idx.delete_skill
    monkeypatch.setattr(idx, "delete_skill", lambda _skill_id: False)
    editors = _by_name(_build_skill_editor_tools())

    update = editors["update_skill"].invoke(
        {"name": "Deploy", "description": "new", "body": "new body", "reason": "verified"}
    )
    assert update.startswith("Error updating skill")
    assert "old body" in (home / "skills" / "deploy" / "SKILL.md").read_text()
    delete = editors["delete_skill"].invoke({"name": "Deploy", "reason": "verified"})
    assert delete.startswith("Error:") and "confirm deletion" in delete
    assert len(idx.all_skills()) == 1

    monkeypatch.setattr(idx, "delete_skill", original_delete)
    monkeypatch.setattr(idx, "add_skill", lambda *_args, **_kwargs: None)
    reviewer_save = _by_name(_build_curation_tools(provenance_required=True))["save_skill"]
    create = reviewer_save.func(
        "Release",
        "d",
        "b",
        provenance_reason="verified",
        state={"session_id": "session-42"},
    )
    assert create.startswith("Error:") and "did not confirm creation" in create


def test_skill_update_and_delete_archive_outgoing_versions(tmp_path, monkeypatch):
    home = tmp_path / "instance"
    monkeypatch.setenv("PROTOAGENT_HOME", str(home))
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    save_skill = _by_name(_build_curation_tools())["save_skill"]
    save_skill.invoke({"name": "Deploy", "description": "old", "body": "old body"})

    editors = _by_name(_build_skill_editor_tools())
    out = editors["update_skill"].invoke(
        {
            "name": "Deploy",
            "description": "new",
            "body": "new body",
            "reason": "learned retry ordering",
            "source_session_id": "session-42",
        }
    )
    assert "Updated skill" in out and "archived" in out
    assert idx.all_skills()[0]["prompt_template"] == "new body"
    assert idx.all_skills()[0]["source_session_id"] == "session-42"
    history = list((home / "skills" / ".history" / "deploy").glob("*-SKILL.md"))
    assert len(history) == 1 and "old body" in history[0].read_text()

    live_path = home / "skills" / "deploy" / "SKILL.md"
    history_live = live_path.read_bytes().replace(b"\n", b"\r\n") + b"<!-- exact -->\r\n"
    live_path.write_bytes(history_live)
    out = editors["delete_skill"].invoke({"name": "Deploy", "reason": "superseded by release skill"})
    assert "Deleted skill" in out and idx.all_skills() == []
    archived = sorted((home / "skills" / ".history" / "deploy").glob("*-SKILL.md"))
    assert len(archived) == 2
    assert archived[-1].read_bytes() == history_live
    assert '"reason": "superseded by release skill"' in archived[-1].with_suffix(".json").read_text()


def test_skill_edit_tools_are_guarded_by_binding_flag():
    off = {t.name for t in get_all_tools()}
    on = {t.name for t in get_all_tools(skill_edit_enabled=True)}
    assert {"update_skill", "delete_skill"}.isdisjoint(off)
    assert {"update_skill", "delete_skill"} <= on


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


# ── load_skill "Unavailable in this context" annotation (#3403) ────────────────
#
# The skill's advisory `Relevant tools` is reconciled up front against (a) the tools
# bound to the graph EXECUTING the call — `STATE.graph.bound_tools`, the committed
# graph's authoritative assembled surface, NOT a duplicated static list nor the
# process-global `tool_delta` set that any later build overwrites — and (b) the small
# explicit set of host-config gates that guarantee a named tool refuses
# (onboarding.enabled → the project-registration tool). Progressive disclosure is
# preserved: the body still loads.


class _StubTool:
    """Minimal stand-in for a bound tool object — ``bound_tools`` holds tool objects
    and the availability check reads only their ``.name``."""

    def __init__(self, name):
        self.name = name


class _FakeGraph:
    """Stands in for the committed ``STATE.graph``; carries only the ``bound_tools``
    attribute ``create_agent_graph`` stamps (``list(all_tools)``) — the seam load_skill
    now reconciles against."""

    def __init__(self, bound_tools):
        self.bound_tools = list(bound_tools)


def _bind_graph(monkeypatch, names):
    """Commit a graph whose ``bound_tools`` are exactly ``names`` — the authoritative
    per-invocation surface load_skill reconciles against (``STATE.graph.bound_tools``)."""
    monkeypatch.setattr(STATE, "graph", _FakeGraph(_StubTool(n) for n in names))


def _save_skill_with_tools(idx, name, tools):
    _by_name(_build_curation_tools())["save_skill"].invoke(
        {"name": name, "description": "d", "body": "1. do the thing", "tools": tools}
    )


def _unavailable_line(out: str) -> str | None:
    return next((ln for ln in out.splitlines() if ln.startswith("Unavailable in this context:")), None)


def test_load_skill_flags_tool_absent_from_bound_set(tmp_path, monkeypatch):
    """r1: a declared tool absent from the committed graph's bound toolset is named
    under an upfront unavailable annotation, and the procedure still loads."""
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    monkeypatch.setattr(STATE, "graph_config", None)
    # The authoritative runtime seam: `calculator` is bound this invocation, `ghost_tool` isn't.
    _bind_graph(monkeypatch, ["load_skill", "calculator"])
    _save_skill_with_tools(idx, "Haul run", ["calculator", "ghost_tool"])
    out = load_skill.invoke({"name": "Haul run"})
    assert "Relevant tools: calculator, ghost_tool" in out
    # Only the genuinely-absent tool is flagged (calculator is bound, so it isn't).
    assert _unavailable_line(out) == "Unavailable in this context: ghost_tool (not bound in this context)."
    assert "## Procedure" in out and "1. do the thing" in out  # body preserved
    assert out.index("Unavailable in this context:") < out.index("## Procedure")  # before the procedure


def test_load_skill_no_annotation_when_all_bound_and_no_gate(tmp_path, monkeypatch):
    """r2: all declared tools bound and no hard gate → no annotation; output intact."""
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    monkeypatch.setattr(STATE, "graph_config", None)
    _bind_graph(monkeypatch, ["load_skill", "calculator", "current_time"])
    _save_skill_with_tools(idx, "Simple run", ["calculator", "current_time"])
    out = load_skill.invoke({"name": "Simple run"})
    assert "Unavailable in this context:" not in out
    assert "Relevant tools: calculator, current_time" in out
    assert "## Procedure" in out


def test_load_skill_names_onboarding_gate_when_disabled(tmp_path, monkeypatch):
    """r3: onboarding disabled + a skill requiring the registration tool → the
    configuration-gated refusal is NAMED (not a bare "not bound") before the procedure."""
    from graph.config import LangGraphConfig

    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    # Real config seam: onboarding.enabled=false unbinds the registration tool entirely,
    # and the committed graph's bound_tools reflect exactly what get_all_tools assembled.
    cfg = LangGraphConfig.from_dict({"onboarding": {"enabled": False}})
    monkeypatch.setattr(STATE, "graph_config", cfg)
    _bind_graph(monkeypatch, [t.name for t in get_all_tools(knowledge_store=None, graph_config=cfg)])
    _save_skill_with_tools(idx, "Register repo", ["board_register_project"])
    out = load_skill.invoke({"name": "Register repo"})
    line = _unavailable_line(out)
    assert line is not None and "board_register_project" in line
    assert "onboarding is disabled" in line  # the config reason, not just absence
    assert "not bound in this context" not in line
    assert out.index(line) < out.index("## Procedure")


def test_load_skill_does_not_falsely_flag_bound_registration_tool(tmp_path, monkeypatch):
    """r4: onboarding enabled and the registration tool bound → the reported tool name
    (`board_register_project`) is NOT marked unavailable."""
    from graph.config import LangGraphConfig

    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig.from_dict({"onboarding": {"enabled": True}}))
    _bind_graph(monkeypatch, ["load_skill", "board_register_project"])
    _save_skill_with_tools(idx, "Register", ["board_register_project"])
    out = load_skill.invoke({"name": "Register"})
    assert "Unavailable in this context:" not in out


def test_load_skill_onboarding_gate_tracks_the_real_toolset(tmp_path, monkeypatch):
    """r5: the check consults the REAL assembled toolset, not a stale duplicated list.

    Enabled → `onboard_project` is genuinely bound (`get_all_tools` returns it) and is
    NOT flagged. Disabled → `get_all_tools` omits it, and the annotation names the
    config gate — so if someone re-introduced a static inventory this would diverge."""
    from graph.config import LangGraphConfig

    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)

    cfg_on = LangGraphConfig.from_dict({"onboarding": {"enabled": True}})
    on_names = {t.name for t in get_all_tools(knowledge_store=None, graph_config=cfg_on)}
    assert "onboard_project" in on_names  # the real binding seam, enabled

    cfg_off = LangGraphConfig.from_dict({"onboarding": {"enabled": False}})
    off_names = {t.name for t in get_all_tools(knowledge_store=None, graph_config=cfg_off)}
    assert "onboard_project" not in off_names  # disabled → genuinely unbound

    _save_skill_with_tools(idx, "Onboard", ["onboard_project"])

    monkeypatch.setattr(STATE, "graph_config", cfg_on)
    _bind_graph(monkeypatch, on_names)
    assert "Unavailable in this context:" not in load_skill.invoke({"name": "Onboard"})

    monkeypatch.setattr(STATE, "graph_config", cfg_off)
    _bind_graph(monkeypatch, off_names)
    assert "onboarding is disabled" in load_skill.invoke({"name": "Onboard"})


def test_load_skill_reads_committed_graph_not_the_process_global(tmp_path, monkeypatch):
    """Regression (#3403 review): the check reads the COMMITTED graph's bound_tools, not
    the process-global ``tool_delta`` set that ANY later build overwrites. A cache-warmer,
    test, or reload build that records a different toolset must not flip load_skill's
    answer — the bug was labelling tools against a set bound to another invocation."""
    from graph import tool_delta

    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    monkeypatch.setattr(STATE, "graph_config", None)
    # This invocation's graph binds `calculator` (and NOT `ghost_tool`).
    _bind_graph(monkeypatch, ["load_skill", "calculator"])
    # A *different* graph build happens in the same process and records a toolset that
    # DOES include `ghost_tool`; reading that process-global would wrongly clear the flag.
    tool_delta.reset_for_tests()
    tool_delta.record_toolset(["load_skill", "calculator", "ghost_tool"])
    try:
        _save_skill_with_tools(idx, "Haul run", ["calculator", "ghost_tool"])
        out = load_skill.invoke({"name": "Haul run"})
        # Answer tracks STATE.graph.bound_tools, so ghost_tool is still flagged absent.
        assert _unavailable_line(out) == "Unavailable in this context: ghost_tool (not bound in this context)."
    finally:
        tool_delta.reset_for_tests()


def test_load_skill_makes_no_absence_claim_without_a_committed_graph(tmp_path, monkeypatch):
    """Conservative by design: with no committed graph (bound toolset unknown) and no
    config gate, load_skill does not guess a tool is missing — no false guarantee."""
    idx = SkillsIndex(str(tmp_path / "s.db"))
    monkeypatch.setattr(STATE, "skills_index", idx)
    monkeypatch.setattr(STATE, "graph_config", None)
    monkeypatch.setattr(STATE, "graph", None)  # no committed graph → bound set unknown
    _save_skill_with_tools(idx, "Mystery", ["ghost_tool"])
    out = load_skill.invoke({"name": "Mystery"})
    assert "Unavailable in this context:" not in out
    assert "## Procedure" in out


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
