"""``fleet_status`` / ``fleet_pending`` — the hub agent's fleet read tools (ADR 0135 D8, #5534).

The roster is ``supervisor.status()``, health is ``supervisor.member_health()`` and every member
read goes through ``member_read._fetch_member_json``; each is replaced at that seam, so the tools
run without a live fleet and a test can see exactly which slug and path was read.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

import tools.fleet_diagnostics as fd
import tools.fleet_supervision as fs
import tools.lg_tools as lg_tools
from graph.config import LangGraphConfig

_HOST = {"id": "hub-id", "name": "main", "host": True, "running": True, "version": "1.2.0"}
_ALPHA = {"id": "alpha-1a2b", "name": "Coder", "running": True, "version": "1.2.0"}
_TWIN = {"id": "alpha-9f9f", "name": "Coder", "running": True, "version": "1.1.0"}
_STOPPED = {"id": "idle-0000", "name": "Idle", "running": False, "version": ""}
_REMOTE = {"id": "rem1", "name": "Remote", "remote": True, "running": False, "version": "1.0.0"}

_SECRET = "sk-ant-api03-" + "A" * 40


def _decision(did, question="Ship it?", **extra):
    return {"id": did, "state": "pending", "question": question, "agent": "coder", "session_id": "s1", **extra}


def _install(monkeypatch, roster, members, health=()):
    """Fix the roster + health, and answer member reads from ``members``:
    ``{slug: {"decisions": [...], "issues": [...], "model": "name"}}``; a slug that is absent
    is unreachable (the helper's None). Returns the ``(slug, path)`` reads made, and the thread
    each roster read ran on."""
    from graph.fleet import member_read, supervisor

    calls: list[tuple[str, str]] = []
    threads: list[int] = []

    def status():
        threads.append(threading.get_ident())
        return [dict(e) for e in roster]

    async def fetch(slug, path):
        calls.append((slug, path))
        data = members.get(slug)
        if data is None:
            return None
        if path.startswith("api/decisions"):
            rows = data.get("decisions")
            return None if rows is None else {"decisions": rows, "pending": data.get("pending", len(rows))}
        if path == "api/tasks/issues":
            rows = data.get("issues")
            return None if rows is None else {"issues": rows}
        if path == "api/runtime/status":
            return {"model": {"name": data.get("model", "")}, "version": "ignored"}
        raise AssertionError(f"unexpected member path {path!r}")

    monkeypatch.setattr(supervisor, "status", status)
    monkeypatch.setattr(supervisor, "member_health", lambda: [dict(h) for h in health])
    monkeypatch.setattr(member_read, "_fetch_member_json", fetch)
    return calls, threads


def _by_id(result):
    return {m["id"]: m for m in result["members"]}


def _names(config=None):
    return {getattr(t, "name", "") for t in lg_tools.get_all_tools(graph_config=config)}


# ── binding: one existing flag, hub only ──────────────────────────────────────


def test_tools_are_absent_by_default_and_bound_by_the_existing_flag():
    assert not {"fleet_status", "fleet_pending"} & _names()
    assert not {"fleet_status", "fleet_pending"} & _names(LangGraphConfig())
    on = _names(LangGraphConfig(tools_fleet_diagnostics_enabled=True))
    assert {"fleet_status", "fleet_pending", "fleet_diagnostics"} <= on


def test_tools_are_absent_inside_a_workspace_member(monkeypatch):
    from graph.workspaces import manager

    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    on = _names(LangGraphConfig(tools_fleet_diagnostics_enabled=True))
    assert not {"fleet_status", "fleet_pending"} & on
    assert "fleet_diagnostics" in on  # the existing tool's binding is unchanged


def test_tools_take_no_arguments():
    for t in fs.build_fleet_supervision_tools():
        assert t.args == {}, t.name


def test_no_config_key_was_added():
    assert not [f for f in LangGraphConfig.__dataclass_fields__ if "fleet_supervision" in f or "fleet_status" in f]


# ── federation / public callers ───────────────────────────────────────────────


class _Request:
    def __init__(self, tools):
        self.tools = tools

    def override(self, **kw):
        return _Request(kw["tools"])


@pytest.mark.parametrize("restricted,expected", [(True, {"fleet_diagnostics"}), (False, None)])
def test_federation_and_public_turns_lose_both_tools(monkeypatch, restricted, expected):
    from graph.middleware import knowledge

    assert {"fleet_status", "fleet_pending"} <= knowledge._FEDERATION_DENIED_TOOLS
    monkeypatch.setattr(knowledge, "_restrict_prior_session_memory", lambda: restricted)
    bound = lg_tools.get_all_tools(graph_config=LangGraphConfig(tools_fleet_diagnostics_enabled=True))
    out = knowledge.KnowledgeMiddleware._scrub_federation_tools(_Request(bound))
    left = {t.name for t in out.tools}
    fleet = {n for n in left if n.startswith("fleet_")}
    assert fleet == (expected or {"fleet_status", "fleet_pending", "fleet_diagnostics"})


# ── fleet_status ──────────────────────────────────────────────────────────────


async def test_fleet_status_reports_roster_health_model_and_counts(monkeypatch):
    health = [
        {"id": "alpha-1a2b", "name": "Coder", "kind": "local", "state": "wedged", "changed_at": "2026-11-18T10:00:00+00:00"},
        {"id": "rem1", "name": "Remote", "kind": "remote", "state": "healthy", "changed_at": "2026-11-18T09:00:00+00:00"},
    ]
    members = {
        "host": {"decisions": [], "issues": [{"id": "t1", "status": "open"}], "model": "hub-model"},
        "alpha-1a2b": {
            "decisions": [_decision("d1")],
            "pending": 3,
            "issues": [{"id": "t1", "status": "waiting"}, {"id": "t2", "status": "in_progress"}],
            "model": "coder-model",
        },
        "rem1": {"decisions": [], "issues": None, "model": "remote-model"},
    }
    calls, threads = _install(monkeypatch, [_HOST, _ALPHA, _STOPPED, _REMOTE], members, health)

    tool = {t.name: t for t in fs.build_fleet_supervision_tools()}["fleet_status"]
    rows = _by_id(await tool.ainvoke({}))

    alpha = rows["alpha-1a2b"]
    assert (alpha["running"], alpha["healthy"], alpha["health"]) == (True, False, "wedged")
    assert alpha["health_changed_at"] == "2026-11-18T10:00:00+00:00"
    assert (alpha["version"], alpha["model"]) == ("1.2.0", "coder-model")
    assert (alpha["pending_decisions"], alpha["waiting_tasks"]) == (3, 1)
    # The host is read through the reserved ``host`` slug and has no health record.
    assert rows["hub-id"]["kind"] == "host" and rows["hub-id"]["model"] == "hub-model"
    assert (rows["hub-id"]["healthy"], rows["hub-id"]["health"]) == (None, "unknown")
    assert (rows["hub-id"]["pending_decisions"], rows["hub-id"]["waiting_tasks"]) == (0, 0)
    # A remote is read even when its cached probe says down; a failed task read is None, not 0.
    assert rows["rem1"]["healthy"] is True and rows["rem1"]["reachable"] is True
    assert (rows["rem1"]["pending_decisions"], rows["rem1"]["waiting_tasks"]) == (0, None)
    # A stopped local member is reported from the roster alone — no member read at all.
    stopped = rows["idle-0000"]
    assert (stopped["running"], stopped["reachable"], stopped["model"]) == (False, None, None)
    assert "idle-0000" not in {slug for slug, _ in calls}
    assert {slug for slug, _ in calls} == {"host", "alpha-1a2b", "rem1"}
    # The roster read (file lock + registry) ran off the event loop.
    assert threads and threading.get_ident() not in threads


async def test_fleet_status_marks_an_unreachable_member_without_failing(monkeypatch):
    _install(monkeypatch, [_ALPHA, _TWIN], {"alpha-9f9f": {"decisions": [], "issues": [], "model": "m"}})

    rows = _by_id(await fs.fleet_status())

    assert rows["alpha-1a2b"]["reachable"] is False
    assert (rows["alpha-1a2b"]["pending_decisions"], rows["alpha-1a2b"]["waiting_tasks"]) == (None, None)
    assert rows["alpha-9f9f"]["reachable"] is True


# ── fleet_pending ─────────────────────────────────────────────────────────────


async def test_fleet_pending_lists_decisions_and_waiting_tasks_per_member(monkeypatch):
    members = {
        "alpha-1a2b": {
            "decisions": [_decision("d1", options=[{"id": "y", "label": "Yes", "description": "long"}])],
            "issues": [
                {"id": "t1", "status": "waiting", "title": "Pick a vendor", "ask": "A or B?", "description": "x" * 50},
                {"id": "t2", "status": "open", "title": "Not waiting"},
            ],
        },
        "alpha-9f9f": {"decisions": [], "issues": []},
    }
    calls, _ = _install(monkeypatch, [_ALPHA, _TWIN, _STOPPED, _REMOTE], members)

    tool = {t.name: t for t in fs.build_fleet_supervision_tools()}["fleet_pending"]
    result = await tool.ainvoke({})
    rows = _by_id(result)

    # Two members share the display name "Coder" and stay two distinct rows.
    assert [m["name"] for m in result["members"]].count("Coder") == 2
    assert rows["alpha-1a2b"]["status"] == "ok" and rows["alpha-9f9f"]["status"] == "ok"
    assert rows["alpha-1a2b"]["decisions"] == [
        {"id": "d1", "session_id": "s1", "agent": "coder", "question": "Ship it?", "options": [{"id": "y", "label": "Yes"}]}
    ]
    assert rows["alpha-1a2b"]["tasks"] == [{"id": "t1", "title": "Pick a vendor", "ask": "A or B?", "status": "waiting"}]
    assert rows["alpha-9f9f"]["decisions"] == [] and rows["alpha-9f9f"]["tasks"] == []
    # Unreachable and stopped members are reported, and the call still answers.
    assert rows["rem1"]["status"] == "unreachable" and "decisions" not in rows["rem1"]
    assert rows["idle-0000"]["status"] == "stopped"
    assert result["ok"] is True and result["unreachable"] == ["rem1"]
    assert (result["pending_decisions"], result["waiting_tasks"]) == (1, 1)
    # Every read went through the shared helper, with the member's own pending filter.
    assert ("alpha-1a2b", f"api/decisions?state=pending&limit={fs._MAX_ITEMS}") in calls
    assert ("alpha-1a2b", "api/tasks/issues") in calls


async def test_fleet_pending_redacts_then_bounds_member_text(monkeypatch):
    # The secret straddles the field cap: capping first would leak its head.
    question = "q" * (fd._TOOL_TEXT_CAP - 10) + " " + _SECRET
    members = {
        "alpha-1a2b": {
            "decisions": [_decision("d0", question=question)],
            "issues": [
                {"id": f"t{i}", "status": "waiting", "title": f"key {_SECRET}", "token": "hunter2"}
                for i in range(fs._MAX_ITEMS + 5)
            ],
        }
    }
    _install(monkeypatch, [_ALPHA], members)

    row = (await fs.fleet_pending())["members"][0]

    assert _SECRET[:20] not in repr(row) and "hunter2" not in repr(row)
    assert len(row["decisions"][0]["question"]) <= fd._TOOL_TEXT_CAP
    assert len(row["tasks"]) == fs._MAX_ITEMS == fd._MAX_TASK_HISTORY
    assert row["waiting_tasks"] == fs._MAX_ITEMS + 5  # the count stays honest
    assert row["tool_truncated"] is True


async def test_fleet_pending_keeps_a_failed_read_distinct_from_empty(monkeypatch):
    # Decisions answer, tasks do not (no task store on that member → the helper's None).
    _install(monkeypatch, [_ALPHA], {"alpha-1a2b": {"decisions": [_decision("d1")], "pending": 9, "issues": None}})

    row = (await fs.fleet_pending())["members"][0]

    assert row["status"] == "ok" and row["pending_decisions"] == 9
    assert row["tasks"] is None and row["waiting_tasks"] is None
    assert row["tool_truncated"] is True  # 9 pending, 1 listed


async def test_a_raising_member_read_or_health_read_is_contained(monkeypatch):
    from graph.fleet import member_read, supervisor

    _install(monkeypatch, [_ALPHA], {})

    async def boom(slug, path):
        raise RuntimeError("registry exploded")

    def no_health():
        raise RuntimeError("health exploded")

    monkeypatch.setattr(member_read, "_fetch_member_json", boom)
    monkeypatch.setattr(supervisor, "member_health", no_health)

    pending, status = await asyncio.gather(fs.fleet_pending(), fs.fleet_status())

    assert pending["members"][0]["status"] == "unreachable"
    assert status["members"][0]["reachable"] is False and status["members"][0]["health"] == "unknown"
