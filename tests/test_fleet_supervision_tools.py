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
    monkeypatch.setattr(supervisor, "fresh_member_health", lambda max_age, **kw: [])
    monkeypatch.setattr(supervisor, "member_health", lambda: [dict(h) for h in health])
    monkeypatch.setattr(member_read, "_fetch_member_json", fetch)
    return calls, threads


def _by_id(result):
    return {m["id"]: m for m in result["members"]}


def _names(config=None):
    return {getattr(t, "name", "") for t in lg_tools.get_all_tools(graph_config=config)}


# ── binding: one existing flag, hub only ──────────────────────────────────────


def test_tools_are_absent_by_default_and_bound_by_the_existing_flag():
    assert not {"fleet_status", "fleet_pending", "fleet_dismiss"} & _names()
    assert not {"fleet_status", "fleet_pending", "fleet_dismiss"} & _names(LangGraphConfig())
    on = _names(LangGraphConfig(tools_fleet_diagnostics_enabled=True))
    assert {"fleet_status", "fleet_pending", "fleet_dismiss", "fleet_diagnostics"} <= on


def test_tools_are_absent_inside_a_workspace_member(monkeypatch):
    from graph.workspaces import manager

    monkeypatch.setattr(manager, "is_workspace_member", lambda: True)
    on = _names(LangGraphConfig(tools_fleet_diagnostics_enabled=True))
    assert not {"fleet_status", "fleet_pending", "fleet_dismiss"} & on
    assert "fleet_diagnostics" in on  # the existing tool's binding is unchanged


def test_tools_take_no_arguments():
    for t in fs.build_fleet_supervision_tools():
        if t.name != "fleet_dismiss":
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

    assert {"fleet_status", "fleet_pending", "fleet_dismiss"} <= knowledge._FEDERATION_DENIED_TOOLS
    monkeypatch.setattr(knowledge, "_restrict_prior_session_memory", lambda: restricted)
    bound = lg_tools.get_all_tools(graph_config=LangGraphConfig(tools_fleet_diagnostics_enabled=True))
    out = knowledge.KnowledgeMiddleware._scrub_federation_tools(_Request(bound))
    left = {t.name for t in out.tools}
    fleet = {n for n in left if n.startswith("fleet_")}
    assert fleet == (expected or {"fleet_status", "fleet_pending", "fleet_dismiss", "fleet_diagnostics"})


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


async def test_status_refreshes_real_supervisor_health_before_any_background_sweep(tmp_path, monkeypatch):
    from graph.fleet import member_read, supervisor
    from graph.workspaces import manager

    monkeypatch.setenv("PROTOAGENT_WORKSPACES_DIR", str(tmp_path / "ws"))
    monkeypatch.setattr(supervisor, "_health", {})
    monkeypatch.setattr(supervisor, "_last_sweep", None)
    monkeypatch.setattr(supervisor, "_sweep_threshold", None)
    monkeypatch.setattr(supervisor, "_unclaimed_changes", [])
    monkeypatch.setattr(supervisor, "_probe_cache", {})
    monkeypatch.setattr(supervisor, "_alive", lambda pid: pid == 123)
    monkeypatch.setattr(supervisor, "_host_entry", lambda: dict(_HOST))
    ws = manager.create("coder", port=7879)
    supervisor._save_state({ws["id"]: {"pid": 123, "port": 7879, "version": "1.2.0"}})
    remote = supervisor.add_remote("remote", "http://remote:7870")
    monkeypatch.setattr(supervisor, "_PROBE_TTL", 0)
    threads = []
    healthy = True

    def probe(rec, timeout, **kw):
        threads.append(threading.get_ident())
        supervisor._probe_cache[rec["id"]] = (healthy, 0.0)
        return healthy, ""

    async def fetch(slug, path):
        return None

    monkeypatch.setattr(supervisor, "_probe_one", probe)
    monkeypatch.setattr(member_read, "_fetch_member_json", fetch)
    assert supervisor.member_health() == []
    tool = fs.build_fleet_supervision_tools()[0]
    first = _by_id(await tool.ainvoke({}))
    again = _by_id(await tool.ainvoke({}))
    for mid in (ws["id"], remote["id"]):
        assert first[mid]["healthy"] is True
        assert first[mid]["health_changed_at"]
        assert again[mid]["health_changed_at"] == first[mid]["health_changed_at"]
    assert first[remote["id"]]["running"] is True

    # One failed check shows in this read, but it is not the supervisor's verdict: the
    # records are shared with the health loop and the fleet:member_health watch check, so a
    # roster read must not mark a live member wedged under a lower threshold of its own.
    healthy = False
    failed = _by_id(await tool.ainvoke({}))
    for mid in (ws["id"], remote["id"]):
        assert (failed[mid]["health"], failed[mid]["healthy"], failed[mid]["failed_checks"]) == ("failing", False, 1)
        assert failed[mid]["health_changed_at"] == first[mid]["health_changed_at"]
    assert failed[remote["id"]]["running"] is False
    assert {h["id"]: h["state"] for h in supervisor.member_health()} == {ws["id"]: "healthy", remote["id"]: "healthy"}
    await tool.ainvoke({})
    # The third failed check in a row is the supervisor's threshold: now it is wedged / down.
    third = _by_id(await tool.ainvoke({}))
    assert (third[ws["id"]]["health"], third[remote["id"]]["health"]) == ("wedged", "down")
    assert third[ws["id"]]["failed_checks"] == 3 and third[ws["id"]]["healthy"] is False
    healthy = True
    recovered = _by_id(await tool.ainvoke({}))
    for mid in (ws["id"], remote["id"]):
        assert (recovered[mid]["healthy"], recovered[mid]["failed_checks"]) == (True, 0)
        assert recovered[mid]["health_changed_at"] != first[mid]["health_changed_at"]
    assert threads and threading.get_ident() not in threads
    # A roster read only reads: every change it found is still there for the loop to claim.
    claimed = [(c["id"], c["previous"], c["state"]) for c in supervisor.health_sweep(3)]
    assert claimed == [
        (ws["id"], "healthy", "wedged"), (remote["id"], "healthy", "down"),
        (ws["id"], "wedged", "healthy"), (remote["id"], "down", "healthy"),
    ]


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


@pytest.mark.parametrize("health_failure", ["member_health", "fresh_member_health"])
async def test_a_raising_member_read_or_health_read_is_contained(monkeypatch, health_failure):
    from graph.fleet import member_read, supervisor

    _install(monkeypatch, [_ALPHA], {})

    async def boom(slug, path):
        raise RuntimeError("registry exploded")

    def no_health(*a, **kw):
        raise RuntimeError("health exploded")

    monkeypatch.setattr(member_read, "_fetch_member_json", boom)
    monkeypatch.setattr(supervisor, health_failure, no_health)

    pending, status = await asyncio.gather(fs.fleet_pending(), fs.fleet_status())

    assert pending["members"][0]["status"] == "unreachable"
    assert status["members"][0]["reachable"] is False and status["members"][0]["health"] == "unknown"


@pytest.fixture
def dismiss_env(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from graph.fleet import proxy, service_token, supervisor
    from observability.audit import AuditLogger

    roster = [dict(_ALPHA), dict(_TWIN), dict(_STOPPED), dict(_REMOTE)]
    monkeypatch.setattr(supervisor, "status", lambda: roster)
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: (f"http://{slug}", {}))
    monkeypatch.setattr(proxy, "_remote_slugs", {"rem1"})
    monkeypatch.setattr(service_token, "resolve_service_token", lambda: "fleet-test-token")
    audit = AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr("observability.audit.audit_logger", audit)
    tool = {t.name: t for t in fs.build_fleet_supervision_tools()}["fleet_dismiss"]
    return SimpleNamespace(tool=tool, audit=audit, roster=roster)


async def _dismiss(env, **overrides):
    return await env.tool.ainvoke({
        "member": _ALPHA["id"], "kind": "decision", "item_id": "stale",
        "reason": "No longer needed", "state": {"operator_chat_turn": True, "session_id": "chat"},
        **overrides,
    })


async def test_dismiss_service_token_cannot_cancel_decisions_but_can_close_tasks(dismiss_env, monkeypatch, tmp_path):
    import httpx
    from fastapi import FastAPI
    from a2a_impl import auth
    from graph.decisions import DecisionStore
    from graph.fleet import proxy
    from operator_api.decision_routes import register_decision_routes
    from operator_api.routes import register_operator_routes
    from runtime.state import STATE
    from tasks.store import TaskStore

    decisions = DecisionStore(":memory:")
    decision = decisions.create("job:scheduled", "Still needed?", ["yes", "no"])
    tasks = TaskStore(db_path=str(tmp_path / "tasks.db"))
    task = tasks.create("Stale ask")
    tasks.update(task["id"], status="waiting")
    monkeypatch.setattr(STATE, "decision_store", decisions)

    async def not_parked(*args):
        return False

    monkeypatch.setattr("graph.decision_reconcile.interrupt_parked", not_parked)
    app = FastAPI()
    auth.install(app, bearer_token="operator-test-token", fleet_token="fleet-test-token",
                 api_key="", allowed_origins_raw="")
    register_decision_routes(app)
    notices = []

    async def inbox_add(payload):
        assert tasks.get(task["id"])["status"] == "closed"
        notices.append(payload)

    register_operator_routes(app, runtime_status=lambda: {}, subagent_list=lambda: [],
                             subagent_run=lambda req: "", subagent_batch=lambda req: "",
                             tasks_store=tasks, inbox_add=inbox_add)
    sent = []

    async def capture(request):
        sent.append(request)
        assert request.headers["authorization"] == "Bearer fleet-test-token"
        assert request.extensions["timeout"]["read"] == 10

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), event_hooks={"request": [capture]}) as client:
        monkeypatch.setattr(proxy, "_get_client", lambda: client)
        before = _by_id(await fs.fleet_pending())[_ALPHA["id"]]
        assert before["pending_decisions"] == before["waiting_tasks"] == 1
        refused = await _dismiss(dismiss_env, item_id=decision.id)
        assert refused["ok"] is False and refused["status_code"] == 403
        assert (await _dismiss(dismiss_env, kind="task", item_id=task["id"]))["ok"] is True
        after = _by_id(await fs.fleet_pending())[_ALPHA["id"]]
        assert after["pending_decisions"] == 1 and after["waiting_tasks"] == 0
    assert len(notices) == 1 and "Outcome: dismissed" in notices[0]["text"]
    assert decisions.get(decision.id).pending
    closed = tasks.get(task["id"])
    assert closed["status"] == "closed" and closed["close_reason"] == "dismissed: No longer needed"
    assert [r.url.path for r in sent if r.method == "POST"] == [
        f"/api/decisions/{decision.id}/cancel", f"/api/tasks/issues/{task['id']}/close",
    ]
    entries = dismiss_env.audit.get_recent()
    assert len(entries) == 2
    assert sorted(e["success"] for e in entries) == [False, True]
    assert all(e["args"]["reason"] == "No longer needed" for e in entries)


@pytest.mark.parametrize("overrides,error", [
    ({"state": {}}, "operator_turn_required"),
    ({"state": {"operator_chat_turn": False}}, "operator_turn_required"),
    ({"state": {"operator_chat_turn": True, "subagent_fence": True}}, "operator_turn_required"),
    ({"reason": " \n"}, "reason_required"),
    ({"member": _STOPPED["id"]}, "unreachable"),
    ({"member": "Coder"}, "unknown_member"),
    ({"kind": "answer"}, "invalid_kind"),
    ({"item_id": ".."}, "invalid_item_id"),
])
async def test_dismiss_refusals_are_audited_without_requests(dismiss_env, monkeypatch, overrides, error):
    from graph.fleet import proxy
    requests = []
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: requests.append(slug))
    result = await _dismiss(dismiss_env, **overrides)
    assert result == {"ok": False, "error": error}
    assert requests == []
    entries = dismiss_env.audit.get_recent()
    assert len(entries) == 1
    assert entries[0]["args"] == {"member": overrides.get("member", _ALPHA["id"]),
                                  "kind": overrides.get("kind", "decision"),
                                  "id": overrides.get("item_id", "stale"),
                                  "reason": overrides.get("reason", "No longer needed")}
    assert error in entries[0]["result_summary"] and not entries[0]["success"]


@pytest.mark.parametrize("failure,error", [(404, "not_found"), (409, "member_error"),
                                            ("timeout", "unreachable"), ("resolution", "unreachable")])
async def test_dismiss_member_failures_are_contained(dismiss_env, monkeypatch, failure, error):
    import httpx
    from graph.fleet import proxy

    def handle(request):
        if request.url.host == _ALPHA["id"]:
            if failure == "timeout":
                raise httpx.ReadTimeout("private detail")
            return httpx.Response(failure, text="private detail")
        return httpx.Response(200)

    if failure == "resolution":
        def target(slug):
            if slug == _ALPHA["id"]:
                raise KeyError("port")
            return f"http://{slug}", {}
        monkeypatch.setattr(proxy, "_target_for_slug", target)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(proxy, "_get_client", lambda: client)
        result = await _dismiss(dismiss_env)
        assert result["error"] == error and result["ok"] is False
        assert "private detail" not in str(result)
        assert (await _dismiss(dismiss_env, member=_TWIN["id"]))["ok"] is True
    entries = dismiss_env.audit.get_recent()
    assert len(entries) == 2
    assert sum(e["success"] for e in entries) == 1


@pytest.mark.parametrize("bearer", ["stored-remote-token", ""])
async def test_dismiss_remote_uses_only_stored_credentials(dismiss_env, monkeypatch, bearer):
    import httpx
    from graph.fleet import proxy
    headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
    monkeypatch.setattr(proxy, "_target_for_slug", lambda slug: ("http://remote", headers))
    sent = []

    def handle(request):
        sent.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(proxy, "_get_client", lambda: client)
        assert (await _dismiss(dismiss_env, member="rem1"))["ok"] is True
    assert len(sent) == 1
    assert sent[0].headers.get("authorization", "") == (f"Bearer {bearer}" if bearer else "")


@pytest.mark.parametrize("status", ["open", "in_progress", "blocked", "deferred", "closed", "missing", "resumed"])
async def test_dismiss_preserves_non_waiting_tasks(dismiss_env, monkeypatch, tmp_path, status):
    import httpx
    from fastapi import FastAPI
    from graph.fleet import proxy
    from operator_api.routes import register_operator_routes
    from tasks.store import TaskStore

    tasks = TaskStore(db_path=str(tmp_path / "tasks.db"))
    task = tasks.create("Keep this work")
    task = tasks.update(task["id"], status="waiting" if status == "resumed" else (
        "open" if status == "missing" else status
    ))
    if status == "missing":
        tasks.delete(task["id"])
    preserved = tasks.get(task["id"])

    notices = []

    async def inbox_add(payload):
        notices.append(payload)

    original_close = tasks.close

    def resume_before_close(*args, **kwargs):
        # Resume after the route's read but before its atomic close (#5602).
        nonlocal preserved
        preserved = tasks.update(task["id"], status="in_progress")
        return original_close(*args, **kwargs)

    if status == "resumed":
        monkeypatch.setattr(tasks, "close", resume_before_close)

    app = FastAPI()
    register_operator_routes(app, runtime_status=lambda: {}, subagent_list=lambda: [],
                             subagent_run=lambda req: "", subagent_batch=lambda req: "",
                             tasks_store=tasks, inbox_add=inbox_add)
    sent = []

    async def capture(request):
        sent.append(request.method)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), event_hooks={"request": [capture]}) as client:
        monkeypatch.setattr(proxy, "_get_client", lambda: client)
        result = await _dismiss(dismiss_env, kind="task", item_id=task["id"])
    expected = "member_error" if status == "resumed" else "not_found" if status == "missing" else "task_not_waiting"
    assert result["ok"] is False and result["error"] == expected
    assert sent == (["GET", "POST"] if status == "resumed" else ["GET"])
    if status == "resumed":
        assert result["status_code"] == 409
    assert tasks.get(task["id"]) == preserved
    assert notices == []
    entries = dismiss_env.audit.get_recent()
    assert len(entries) == 1 and not entries[0]["success"]
    assert expected in entries[0]["result_summary"]
    tasks._conn.close()


@pytest.mark.parametrize("response", [None, {}, {"issues": None}])
async def test_dismiss_task_requires_a_successful_status_read(dismiss_env, monkeypatch, response):
    from graph.fleet import member_read

    async def read(slug, path):
        return response

    async def post(*args):
        pytest.fail("An unreadable task must not be closed")

    monkeypatch.setattr(member_read, "_fetch_member_json", read)
    monkeypatch.setattr(member_read, "_post_member_json", post)
    assert await _dismiss(dismiss_env, kind="task") == {"ok": False, "error": "unreachable"}
    entries = dismiss_env.audit.get_recent()
    assert len(entries) == 1 and not entries[0]["success"]
