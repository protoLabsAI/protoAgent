"""Hub recovery uses the shared lifecycle ops and ordinary approval flow."""

import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from graph.config import LangGraphConfig
from graph.fleet import proxy, supervisor
from graph.middleware import knowledge
from graph.middleware.request_context import verified_principal_scope
from observability.audit import AuditLogger
from server.turn_control import _AUTONOMOUS_HITL_SENTINEL
from tools import fleet_supervision as fs
from tools.lg_tools import get_all_tools


_load_restarts = supervisor._load_restarts


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    main_thread = threading.get_ident()
    roster = [{"id": "worker-1234", "name": "Worker", "running": False}]
    stopped = set()
    events = []

    def status():
        assert threading.get_ident() != main_thread
        return [dict(row) for row in roster]

    def start(ident):
        assert threading.get_ident() != main_thread
        events.append(("start", ident))
        return {"running": True}

    def stop(ident):
        events.append(("stop", ident))
        return {"stopped": True}

    monkeypatch.setattr(supervisor, "status", status)
    monkeypatch.setattr(supervisor, "_resolve_key", lambda ident: ident)
    monkeypatch.setattr(supervisor, "_load_restarts", lambda: {"members": {s: {"stopped": True} for s in stopped}})
    monkeypatch.setattr(supervisor, "_shutting_down", False)
    monkeypatch.setattr(supervisor, "_unsaved_stops", set())
    monkeypatch.setattr(supervisor, "start", start)
    monkeypatch.setattr(supervisor, "stop", stop)
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: False)
    audit = AuditLogger(tmp_path / "audit.jsonl")
    monkeypatch.setattr("observability.audit.audit_logger", audit)
    approval = Mock(return_value=True)
    monkeypatch.setattr(fs, "interrupt", approval)
    cfg = LangGraphConfig(fleet_member_restart_enabled=True)
    tool = fs.build_fleet_member_restart_tools(cfg)[0]
    return SimpleNamespace(roster=roster, stopped=stopped, events=events, audit=audit,
                           approval=approval, tool=tool, cfg=cfg)


async def invoke(env, **args):
    return await env.tool.ainvoke({"member": "worker-1234", "reason": "Recover stale worker",
                                  "state": {"session_id": "chat"}, **args})


@pytest.mark.asyncio
@pytest.mark.parametrize("running,idle", [(False, True), (True, True), (True, False), (True, None)])
async def test_shared_ops_start_or_restart_only_when_idle(recovery, monkeypatch, running, idle):
    env = recovery
    env.roster[0]["running"] = running
    monkeypatch.setattr(proxy, "_resolve_slug", lambda slug: ("local", "http://member", None))
    monkeypatch.setattr("graph.fleet.service_token.resolve_service_token", lambda: "service")

    def readiness(request):
        env.events.append(("readiness", request.url.path))
        assert request.headers["authorization"] == "Bearer service"
        return httpx.Response(503 if idle is None else 200, json={"idle": idle})

    async with httpx.AsyncClient(transport=httpx.MockTransport(readiness)) as client:
        monkeypatch.setattr(proxy, "_get_client", lambda: client)
        result = await invoke(env)
    assert result["ok"] is (not running or idle is True)
    expected = [("readiness", "/api/restart/readiness")] if running else []
    if not running or idle is True:
        if running:
            expected.append(("stop", "worker-1234"))
        expected.append(("start", "worker-1234"))
    assert env.events == expected
    entry = env.audit.get_recent()[-1]
    assert entry["args"]["reason"] == "Recover stale worker"
    assert entry["session_id"] == "chat"
    assert entry["success"] == result["ok"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case,error", [
    ("remote", "local_member_required"), ("host", "local_member_required"),
    ("stopped", "stopped_on_purpose"), ("missing", "unknown_member"),
    ("ambiguous", "ambiguous_member"), ("reason", "reason_required"),
    ("disabled", "disabled"),
])
async def test_refusals_are_audited_without_approval_or_mutation(recovery, case, error):
    env = recovery
    if case in {"remote", "host"}:
        env.roster[0][case] = True
    elif case == "stopped":
        env.stopped.add("worker-1234")
    elif case == "missing":
        env.roster.clear()
    elif case == "ambiguous":
        env.roster.append({"id": "worker-1234", "name": "Worker"})
    elif case == "disabled":
        env.cfg.fleet_member_restart_enabled = False
    result = await invoke(env, **({"reason": " "} if case == "reason" else {}))
    assert result["error"] == error
    assert not env.events
    env.approval.assert_not_called()
    assert env.audit.get_recent()[-1]["success"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("after_approval", [False, True])
@pytest.mark.parametrize("running", [False, True])
async def test_unreadable_stop_history_refuses_recovery(recovery, monkeypatch, tmp_path, after_approval, running):
    # #5534: lost operator stop intent must not allow either start or restart.
    history = tmp_path / "fleet-restarts.json"
    monkeypatch.setattr(supervisor, "_restart_path", lambda: history)
    monkeypatch.setattr(supervisor, "_load_restarts", _load_restarts)
    recovery.roster[0]["running"] = running

    def corrupt_history(card=None):
        history.write_text("{unreadable")
        return True

    if after_approval:
        recovery.approval.side_effect = corrupt_history
    else:
        corrupt_history()
    result = await invoke(recovery)
    assert result == {"ok": False, "error": "restart_history_unreadable"}
    assert not recovery.events
    assert recovery.approval.call_count == int(after_approval)
    entry = recovery.audit.get_recent()[-1]
    assert entry["success"] is False
    assert entry["args"]["reason"] == "Recover stale worker"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "remove", "rename"])
async def test_rechecks_id_and_stop_intent_after_approval(recovery, change):
    env = recovery

    def approve(card):
        if change == "stop":
            env.stopped.add("worker-1234")
        elif change == "remove":
            env.roster.clear()
        else:
            env.roster[0]["name"] = "Renamed"
            env.roster.append({"id": "replacement", "name": "Worker"})
        return True

    env.approval.side_effect = approve
    result = await invoke(env)
    assert result["ok"] is (change == "rename")
    assert env.events == ([("start", "worker-1234")] if change == "rename" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [True, _AUTONOMOUS_HITL_SENTINEL])
async def test_real_interrupt_parks_and_unattended_answer_denies(recovery, monkeypatch, decision):
    from langgraph.types import interrupt

    monkeypatch.setattr(fs, "interrupt", interrupt)

    async def node(state):
        return {"result": await invoke(recovery)}

    builder = StateGraph(dict)
    builder.add_node("recover", node)
    builder.add_edge(START, "recover")
    builder.add_edge("recover", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "recovery"}}
    parked = await graph.ainvoke({}, config)
    assert parked["__interrupt__"][0].value["kind"] == "approval"
    assert not recovery.events
    recovery.roster[0]["name"] = "Renamed"
    recovery.roster.append({"id": "replacement", "name": "Worker"})
    result = await graph.ainvoke(Command(resume=decision), config)
    assert result["result"]["ok"] is (decision is True)
    assert recovery.events == ([("start", "worker-1234")] if decision is True else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("running,action", [(False, "start"), (True, "restart")])
async def test_approval_card_names_the_action_for_the_unattended_policy(recovery, running, action):
    # ADR 0135 D2: only "start" (a dead member) is in the hub's unattended allow tier.
    recovery.roster[0]["running"] = running
    recovery.approval.return_value = False
    await invoke(recovery)
    assert recovery.approval.call_args.args[0]["action"] == action


@pytest.mark.asyncio
@pytest.mark.parametrize("came_back", [False, True])
async def test_unattended_start_allowance_does_not_restart_a_member_that_came_back(recovery, came_back):
    from graph.unattended_policy import approval_grant

    def allow(card):
        recovery.roster[0]["running"] = came_back
        return approval_grant({"action": card["action"]})

    recovery.approval.side_effect = allow
    result = await invoke(recovery)
    if came_back:
        assert result == {"ok": False, "error": "state_changed"}
        assert not recovery.events
    else:
        assert result["ok"] is True
        assert recovery.events == [("start", "worker-1234")]


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", ["federation", "public"])
async def test_restricted_callers_lose_tool_and_cannot_invoke_it(recovery, principal):
    class Request:
        tools = [recovery.tool]

        def override(self, **kw):
            return SimpleNamespace(**kw)

    with verified_principal_scope(principal):
        assert knowledge.KnowledgeMiddleware._scrub_federation_tools(Request()).tools == []
        assert (await invoke(recovery))["error"] == "operator_only"
    assert not recovery.events
    recovery.approval.assert_not_called()


@pytest.mark.parametrize("enabled,member,expected", [(False, False, False), (True, False, True), (True, True, False)])
def test_independent_default_off_hub_binding(monkeypatch, enabled, member, expected):
    monkeypatch.setattr("graph.workspaces.manager.is_workspace_member", lambda: member)
    cfg = LangGraphConfig(fleet_member_restart_enabled=enabled)
    assert ("fleet_member_restart" in {t.name for t in get_all_tools(graph_config=cfg)}) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stop_failed", "start_failed", "roster_failed"])
async def test_operation_failures_do_not_claim_success(recovery, monkeypatch, failure):
    from ops import fleet

    async def failed_stop(*args, **kwargs):
        return {"stopped": False}

    def failed(*args):
        raise supervisor.FleetError("unavailable")

    if failure == "stop_failed":
        recovery.roster[0]["running"] = True
        monkeypatch.setattr(fleet, "stop", failed_stop)
    elif failure == "start_failed":
        monkeypatch.setattr(supervisor, "start", failed)
    else:
        monkeypatch.setattr(supervisor, "status", failed)
    result = await invoke(recovery)
    assert result["ok"] is False
    assert not recovery.events
    assert recovery.audit.get_recent()[-1]["success"] is False
