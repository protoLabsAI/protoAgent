"""Operator console handlers (ADR 0023 phase 3) — the bodies behind
register_operator_routes, extracted from _main into operator_api/console_handlers.py.
These exercise the STATE-driven degradation paths directly (no app needed)."""

import json
import logging
from pathlib import Path

import pytest

from operator_api import console_handlers as ch


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    import runtime.state as rs

    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    for field in (
        "graph_config",
        "graph",
        "scheduler",
        "goal_controller",
        "workflow_registry",
        "inbox_store",
        "storm_guard",
        "skills_index",
        "inbox_now_delivery",
    ):
        monkeypatch.setattr(rs.STATE, field, None, raising=False)
    yield


def test_inbox_authorized_open_when_no_token():
    assert ch._inbox_authorized(None) is True
    assert ch._inbox_authorized("anything") is True


def test_inbox_authorized_requires_match(monkeypatch):
    import runtime.state as rs

    class _Cfg:
        auth_token = "secret"

    monkeypatch.setattr(rs.STATE, "graph_config", _Cfg(), raising=False)
    assert ch._inbox_authorized("secret") is True
    assert ch._inbox_authorized("nope") is False
    assert ch._inbox_authorized(None) is False


async def test_scheduler_list_disabled():
    assert await ch._operator_scheduler_list() == {"jobs": [], "backend": "disabled"}


async def test_goals_list_disabled():
    assert await ch._operator_goals_list() == {"goals": [], "enabled": False}


# --- goal completion contracts (ADR 0073) via POST /api/goals handler --------


def _wire_goal_controller(monkeypatch, tmp_path):
    import runtime.state as rs
    from graph.goals.controller import GoalController
    from graph.goals.store import GoalStore

    ctrl = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)
    return ctrl


async def test_goals_set_accepts_contract_and_coerces_string_list(monkeypatch, tmp_path):
    """The operator goal-set handler accepts the ADR 0073 contract fields, coerces a
    bare-string constraint to a 1-element list, and ignores unknown keys."""
    ctrl = _wire_goal_controller(monkeypatch, tmp_path)
    res = await ch._operator_goals_set(
        {
            "session_id": "s1",
            "condition": "ship it",
            "verifier": {"type": "command", "command": "pytest -q"},
            "outcome": "suite green",
            "constraints": "no new deps",  # bare string → 1-element list
            "boundaries": ["graph/", "tools/"],
            "stop_when": "prod access needed",
            "junk": {"ignored": True},  # unknown key ignored
        }
    )
    assert res["ok"] is True
    state = ctrl.active_goal("s1")
    assert state.outcome == "suite green"
    assert state.constraints == ["no new deps"]
    assert state.boundaries == ["graph/", "tools/"]
    assert state.stop_when == "prod access needed"


async def test_goals_set_backward_compatible_without_contract(monkeypatch, tmp_path):
    # A body with only {session_id, condition, verifier} still works — no contract.
    ctrl = _wire_goal_controller(monkeypatch, tmp_path)
    res = await ch._operator_goals_set(
        {"session_id": "s2", "condition": "done", "verifier": {"type": "command", "command": "true"}}
    )
    assert res["ok"] is True
    assert ctrl.active_goal("s2").has_contract is False


async def test_goals_set_kicks_an_initial_drive_turn(monkeypatch, tmp_path):
    # Parity with the chat `/goal` SET (#1910): a successful operator set enqueues a first
    # drive turn so a console-set goal starts immediately.
    _wire_goal_controller(monkeypatch, tmp_path)
    import graph.sdk as sdk

    kicks: list[tuple] = []
    monkeypatch.setattr(sdk, "run_in_session", lambda sid, prompt, **k: (kicks.append((sid, prompt)), {"ok": True})[1])
    res = await ch._operator_goals_set(
        {"session_id": "s9", "condition": "go", "verifier": {"type": "command", "command": "true"}}
    )
    assert res["ok"] is True and res["kicked"] is True
    assert kicks and kicks[0][0] == "s9"


async def test_goals_set_failure_does_not_kick(monkeypatch, tmp_path):
    # A rejected set (no condition) returns an error and never enqueues a turn.
    _wire_goal_controller(monkeypatch, tmp_path)
    import graph.sdk as sdk

    kicks: list = []
    monkeypatch.setattr(sdk, "run_in_session", lambda *a, **k: (kicks.append(a), {"ok": True})[1])
    res = await ch._operator_goals_set({"session_id": "s9", "verifier": {"type": "llm"}})
    assert res["ok"] is False and not kicks


async def test_goals_set_kick_false_skips_the_turn(monkeypatch, tmp_path):
    # The console panel path drives the goal from a chat tab, so it sets `kick: false` — no
    # headless turn is enqueued (the tab owns the drive).
    _wire_goal_controller(monkeypatch, tmp_path)
    import graph.sdk as sdk

    kicks: list = []
    monkeypatch.setattr(sdk, "run_in_session", lambda *a, **k: (kicks.append(a), {"ok": True})[1])
    res = await ch._operator_goals_set(
        {"session_id": "s9", "condition": "go", "verifier": {"type": "llm"}, "kick": False}
    )
    assert res["ok"] is True and res["kicked"] is False and not kicks


async def test_goals_resume_kicks_active_goal(monkeypatch, tmp_path):
    ctrl = _wire_goal_controller(monkeypatch, tmp_path)
    ctrl.set_goal_operator("s1", "cond", {"type": "llm"})
    import graph.sdk as sdk

    kicks: list = []
    monkeypatch.setattr(sdk, "run_in_session", lambda sid, prompt, **k: (kicks.append(sid), {"ok": True})[1])
    res = await ch._operator_goals_resume("s1")
    assert res["ok"] is True and res["kicked"] is True and kicks == ["s1"]


async def test_goals_resume_no_active_goal_is_a_noop(monkeypatch, tmp_path):
    _wire_goal_controller(monkeypatch, tmp_path)
    res = await ch._operator_goals_resume("nope")
    assert res["ok"] is False and "no active goal" in res["error"]


async def test_goals_set_kick_error_does_not_crash(monkeypatch, tmp_path):
    # QA #2091: the best-effort kick must never 500 the request — the goal is already set.
    _wire_goal_controller(monkeypatch, tmp_path)
    import graph.sdk as sdk

    def _boom(*a, **k):
        raise RuntimeError("scheduler down")

    monkeypatch.setattr(sdk, "run_in_session", _boom)
    res = await ch._operator_goals_set({"session_id": "s9", "condition": "go", "verifier": {"type": "llm"}})
    assert res["ok"] is True and res["kicked"] is False  # goal set; kick swallowed


async def test_goals_clear_close_tasks(monkeypatch, tmp_path):
    # Stopping a goal with close_tasks closes its session-scoped task backlog (ADR 0079).
    ctrl = _wire_goal_controller(monkeypatch, tmp_path)
    ctrl.set_goal_operator("s1", "cond", {"type": "llm"})
    import runtime.state as rs

    class _Tasks:
        def __init__(self):
            self.closed: list[str] = []

        def list(self, *, include_closed=True, session_id=None):
            return [{"id": "t1"}, {"id": "t2"}] if session_id == "s1" else []

        def close(self, issue_id, reason=None):
            self.closed.append(issue_id)

    tasks = _Tasks()
    monkeypatch.setattr(rs.STATE, "tasks_store", tasks, raising=False)

    res = await ch._operator_goals_clear("s1", close_tasks=True)
    assert res == {"cleared": True, "tasks_closed": 2} and tasks.closed == ["t1", "t2"]

    # Plain clear leaves tasks alone.
    ctrl.set_goal_operator("s1", "cond", {"type": "llm"})
    tasks.closed.clear()
    res2 = await ch._operator_goals_clear("s1", close_tasks=False)
    assert res2["tasks_closed"] == 0 and tasks.closed == []


def test_as_str_list_coercion():
    assert ch._as_str_list("x") == ["x"]
    assert ch._as_str_list(["a", "b"]) == ["a", "b"]
    assert ch._as_str_list([" ", "b", ""]) == ["b"]  # blank entries dropped
    assert ch._as_str_list("") == []
    assert ch._as_str_list(None) == []
    assert ch._as_str_list({"a": 1}) == []
    assert ch._as_str_list(5) == []


async def test_inbox_add_requires_store():
    with pytest.raises(RuntimeError):
        await ch._operator_inbox_add({"text": "hi"})


async def test_now_inbox_add_uses_registered_a2a_delivery(monkeypatch, tmp_path):
    import runtime.state as rs
    from inbox.store import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    delivered: list[dict] = []
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _delivery(item):
        delivered.append(dict(item))
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _delivery, raising=False)
    res = await ch._operator_inbox_add({"text": "wake up", "priority": "now", "source": "test"})

    assert res["fired"] is True
    assert delivered and delivered[0]["text"] == "wake up"
    assert store.list(priority_floor="later") == []


async def test_now_inbox_add_restores_pending_when_a2a_delivery_rejects(monkeypatch, tmp_path, caplog):
    import runtime.state as rs
    from inbox.store import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    caplog.set_level(logging.WARNING, logger="protoagent.server")

    async def _delivery(_item):
        return False

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _delivery, raising=False)

    res = await ch._operator_inbox_add({"text": "try later", "priority": "now"})

    assert res["fired"] is False
    assert [row["text"] for row in store.list(priority_floor="later")] == ["try later"]
    assert "now-fire not accepted" in caplog.text
    assert "restoring pending fallback" in caplog.text


async def test_now_inbox_add_does_not_fire_when_delivery_cannot_be_reserved(monkeypatch, tmp_path):
    import runtime.state as rs
    from inbox.store import InboxStore

    class _BrokenReserveStore(InboxStore):
        def mark_delivered(self, ids, *, now=None):
            raise RuntimeError("sqlite busy")

    store = _BrokenReserveStore(str(tmp_path / "inbox.db"))
    calls: list[dict] = []
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _delivery(item):
        calls.append(item)
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _delivery, raising=False)

    res = await ch._operator_inbox_add({"text": "reserve first", "priority": "now"})

    assert res["fired"] is False
    assert calls == []
    assert [row["text"] for row in store.list(priority_floor="later")] == ["reserve first"]


async def test_now_inbox_add_stays_pending_without_a2a_delivery_hook(monkeypatch, tmp_path):
    import runtime.state as rs
    from inbox.store import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)
    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", None, raising=False)

    res = await ch._operator_inbox_add({"text": "hook missing", "priority": "now"})

    assert res["fired"] is False
    assert [row["text"] for row in store.list(priority_floor="later")] == ["hook missing"]


async def test_next_and_later_inbox_add_do_not_fire_a2a_delivery(monkeypatch, tmp_path):
    import runtime.state as rs
    from inbox.store import InboxStore

    store = InboxStore(str(tmp_path / "inbox.db"))
    calls: list[dict] = []
    monkeypatch.setattr(rs.STATE, "inbox_store", store, raising=False)

    async def _delivery(item):
        calls.append(item)
        return True

    monkeypatch.setattr(rs.STATE, "inbox_now_delivery", _delivery, raising=False)

    next_res = await ch._operator_inbox_add({"text": "next item", "priority": "next"})
    later_res = await ch._operator_inbox_add({"text": "later item", "priority": "later"})

    assert next_res["fired"] is False and later_res["fired"] is False
    assert calls == []
    assert {row["text"] for row in store.list(priority_floor="later")} == {"next item", "later item"}


def test_chat_commands_lists_workflows_and_subagents(monkeypatch):
    import runtime.state as rs

    class _Reg:
        def list(self):
            return [{"name": "deep-research", "description": "d", "inputs": [{"name": "topic", "required": True}]}]

        def get(self, name):
            return next((w for w in self.list() if w["name"] == name), None)

    monkeypatch.setattr(rs.STATE, "workflow_registry", _Reg(), raising=False)
    out = ch._operator_chat_commands()
    names = [c["name"] for c in out["commands"]]
    assert "deep-research" in names
    dr = next(c for c in out["commands"] if c["name"] == "deep-research")
    assert dr["usage"] == "/deep-research <topic>"


def test_chat_commands_lists_user_facing_skills(monkeypatch):
    """User-facing skills surface as /<slash> commands; non-user-facing skills
    and collisions with a workflow/subagent name are skipped (ADR 0052)."""
    import runtime.state as rs

    class _SkillsIdx:
        def user_facing_skills(self):
            return [
                {"name": "web-research", "description": "Research the web.", "slash": "research"},
                {"name": "Big Task", "description": "Do a big task.", "slash": ""},
            ]

    monkeypatch.setattr(rs.STATE, "skills_index", _SkillsIdx(), raising=False)
    out = ch._operator_chat_commands()
    by_name = {c["name"]: c for c in out["commands"]}
    assert by_name["research"]["usage"] == "/research [input]"
    assert by_name["research"]["description"] == "Research the web."
    assert "big-task" in by_name  # blank slash → slugified name


def test_chat_commands_skill_defers_to_subagent_name(monkeypatch):
    """A user-facing skill whose token collides with a subagent is dropped —
    the subagent owns the slash token in dispatch."""
    import runtime.state as rs
    from graph.subagents.config import SUBAGENT_REGISTRY

    collide = next(iter(SUBAGENT_REGISTRY))  # a real subagent name (e.g. researcher)

    class _SkillsIdx:
        def user_facing_skills(self):
            return [{"name": collide, "description": "shadow", "slash": collide}]

    monkeypatch.setattr(rs.STATE, "skills_index", _SkillsIdx(), raising=False)
    out = ch._operator_chat_commands()
    # The command exists from the subagent, not the skill (skill description dropped).
    cmd = next(c for c in out["commands"] if c["name"] == collide)
    assert cmd["description"] != "shadow"


async def test_runtime_status_carries_plugin_setup_gaps(monkeypatch):
    """A plugin's `report_setup_gap` lands in the operator status `warnings[]` as
    `<Plugin>: <message>` and disappears once cleared — the seam's whole point."""
    from graph.plugins import setup_gaps

    setup_gaps.reset()
    try:
        setup_gaps.report("project_board", "br", "beads CLI 'br' not found on PATH", label="Project Board")
        status = await ch._operator_runtime_status()
        assert "Project Board: beads CLI 'br' not found on PATH" in status["warnings"]
        setup_gaps.report("project_board", "br", None)
        status = await ch._operator_runtime_status()
        assert not [w for w in status["warnings"] if w.startswith("Project Board:")]
    finally:
        setup_gaps.reset()


async def test_runtime_status_publishes_structured_setup_gaps(monkeypatch):
    """Beside the legacy `warnings[]` strings, runtime status carries a typed list of
    the active setup gaps — plugin id, key, display label, message, and the already-
    validated declarative actions — in the store's stable (plugin, key) order."""
    from graph.plugins import setup_gaps

    setup_gaps.reset()
    try:
        setup_gaps.report(
            "project_board",
            "br",
            "beads CLI 'br' not found on PATH",
            label="Project Board",
            action={"kind": "plugin_config", "label": "Open Project Board settings", "fields": ["br_path"]},
        )
        setup_gaps.report("acme", "gh", "gh is not authenticated", label="Acme")
        status = await ch._operator_runtime_status()

        gaps = status["setup_gaps"]
        # Stable (plugin, key) ordering derives from the store — "acme" < "project_board".
        assert [(g["plugin"], g["key"]) for g in gaps] == [("acme", "gh"), ("project_board", "br")]

        board = next(g for g in gaps if g["plugin"] == "project_board")
        assert board["label"] == "Project Board"
        assert board["message"] == "beads CLI 'br' not found on PATH"
        # The action is exposed exactly as the host validated it — `plugin_config` is
        # scoped to the reporting plugin, so `target` is the plugin id.
        assert board["actions"] == [
            {
                "kind": "plugin_config",
                "target": "project_board",
                "label": "Open Project Board settings",
                "fields": ["br_path"],
            }
        ]
        # A gap with no action carries no `actions` key (faithful pass-through).
        acme = next(g for g in gaps if g["plugin"] == "acme")
        assert "actions" not in acme

        # Both projections agree on the same active gaps in the same read.
        assert "Project Board: beads CLI 'br' not found on PATH" in status["warnings"]
        assert "Acme: gh is not authenticated" in status["warnings"]
    finally:
        setup_gaps.reset()


async def test_runtime_status_setup_gaps_empty_when_none(monkeypatch):
    """No gaps → the structured field is present and empty, never absent/None."""
    from graph.plugins import setup_gaps

    setup_gaps.reset()
    status = await ch._operator_runtime_status()
    assert status["setup_gaps"] == []


async def test_runtime_status_setup_gaps_clear_retain_unload_reflected(monkeypatch):
    """Clearing a gap, retaining across a reload, and unloading a plugin each drop the
    affected gaps from BOTH the structured list and the legacy warnings on the next read."""
    from graph.plugins import setup_gaps

    setup_gaps.reset()
    try:
        setup_gaps.report("project_board", "br", "install br", label="Project Board")
        setup_gaps.report("project_board", "delegate", "add a coder delegate", label="Project Board")
        setup_gaps.report("goner", "g", "will be uninstalled", label="Goner")
        status = await ch._operator_runtime_status()
        assert {(g["plugin"], g["key"]) for g in status["setup_gaps"]} == {
            ("project_board", "br"),
            ("project_board", "delegate"),
            ("goner", "g"),
        }

        # Clear one gap (message=None) → gone from both projections; the sibling remains.
        setup_gaps.report("project_board", "br", None)
        status = await ch._operator_runtime_status()
        assert ("project_board", "br") not in {(g["plugin"], g["key"]) for g in status["setup_gaps"]}
        assert not [w for w in status["warnings"] if "install br" in w]

        # Retain across a reload → a plugin no longer present is dropped from both.
        setup_gaps.retain({"project_board"})
        status = await ch._operator_runtime_status()
        assert "goner" not in {g["plugin"] for g in status["setup_gaps"]}
        assert not [w for w in status["warnings"] if w.startswith("Goner:")]

        # Unload the plugin → its remaining gap disappears from both projections.
        setup_gaps.clear_plugin("project_board")
        status = await ch._operator_runtime_status()
        assert status["setup_gaps"] == []
        assert not [w for w in status["warnings"] if w.startswith("Project Board:")]
    finally:
        setup_gaps.reset()


async def test_runtime_status_builds_both_gap_projections_from_one_snapshot(monkeypatch):
    """`warnings[]` and `setup_gaps[]` must describe the SAME instant. The console drops a gap's
    legacy line only when its record is in the same payload; built from two separately locked
    reads, a plugin re-reporting (or clearing) on another thread between them published a line
    with no record — a plain alert the operator could neither act on nor dismiss (#3438 review).

    Deterministic stand-in for that race: every read of the store AFTER the first sees a
    re-report that landed in between, so any handler that reads twice publishes a mismatch."""
    from graph.plugins import setup_gaps

    setup_gaps.reset()
    real_active = setup_gaps.active
    reads = {"n": 0}

    def active_with_a_concurrent_rereport():
        reads["n"] += 1
        if reads["n"] > 1:
            setup_gaps.report("boardy", "coder", f"No coder delegate (check #{reads['n']})", label="Project Board")
        return real_active()

    try:
        setup_gaps.report("boardy", "coder", "No coder delegate (check #1)", label="Project Board")
        monkeypatch.setattr(setup_gaps, "active", active_with_a_concurrent_rereport)
        status = await ch._operator_runtime_status()
        record_lines = {f"{g['label']}: {g['message']}" for g in status["setup_gaps"]}
        gap_lines = [w for w in status["warnings"] if w.startswith("Project Board:")]
        assert gap_lines and set(gap_lines) == record_lines, (gap_lines, record_lines)
    finally:
        monkeypatch.undo()
        setup_gaps.reset()


_CONSOLE_SETUP_GAPS_GOLDEN = Path(__file__).resolve().parents[1] / "apps" / "web" / "e2e" / "setup-gaps.golden.json"


async def test_runtime_status_setup_gaps_match_the_console_e2e_golden(monkeypatch):
    """The console's setup-gap e2e (apps/web/e2e/warnings.spec.ts) serves the payload in
    apps/web/e2e/setup-gaps.golden.json — so it has to be what THIS handler really returns.

    #3421 shipped green against a hand-written mock that put gap objects inside `warnings[]`,
    a shape the server never sends; against the real payload (#3395: records in `setup_gaps[]`,
    plain `Label: message` lines in `warnings[]`) every gap rendered as a plain alert with no
    Configure button and no dismiss. Replaying the golden's `reports` through the real seam and
    asserting its `status` exactly keeps the e2e on the wire shape: change the shape and this
    fails, pointing at the golden to update — and the e2e then runs against the new shape."""
    from graph.plugins import setup_gaps

    golden = json.loads(_CONSOLE_SETUP_GAPS_GOLDEN.read_text(encoding="utf-8"))
    setup_gaps.reset()
    try:
        for r in golden["reports"]:
            setup_gaps.report(r["plugin_id"], r["key"], r["message"], label=r["label"], action=r.get("action"))
        status = await ch._operator_runtime_status()

        assert status["setup_gaps"] == golden["status"]["setup_gaps"]
        # Other operational warnings (a co-located instance, …) may share the array on a dev
        # box; the gap lines are the golden's, in the store's order.
        gap_lines = set(golden["status"]["warnings"])
        assert [w for w in status["warnings"] if w in gap_lines] == golden["status"]["warnings"]
        # The console drops a structured gap's legacy line by exactly this projection
        # (`gapWarningLine` in apps/web/src/app/SetupGapBanner.tsx) — if it drifts, every gap
        # renders twice: once actionable, once as a plain alert.
        assert golden["status"]["warnings"] == [f"{g['label']}: {g['message']}" for g in golden["status"]["setup_gaps"]]
    finally:
        setup_gaps.reset()


async def test_runtime_status_warns_when_self_improvement_has_no_scheduler(monkeypatch):
    from graph.config import LangGraphConfig
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig(self_improvement_enabled=True))
    monkeypatch.setattr(STATE, "scheduler", None)
    status = await ch._operator_runtime_status()
    assert any("Self-improvement reviews are enabled" in warning for warning in status["warnings"])
