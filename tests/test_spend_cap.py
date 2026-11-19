"""Daily unattended spend admission and durable operator continuation (ADR 0135 D12)."""
import asyncio
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.config import LangGraphConfig
from graph.config_io import config_to_dict
from graph.decisions import DecisionStore
from observability.telemetry_store import TelemetryStore
from operator_api.decision_routes import register_decision_routes
from operator_api.telemetry_routes import register_telemetry_routes
from runtime.state import STATE
from scheduler.local import LocalScheduler
from server import turn_control

chat = importlib.import_module("server.chat")


@pytest.fixture
def cap(tmp_path, monkeypatch):
    config = LangGraphConfig(spend_cap_enabled=True, spend_cap_daily_cost_hard_limit=2,
                             spend_cap_daily_token_hard_limit=100)
    telemetry = TelemetryStore(str(tmp_path / "telemetry.db"))
    decisions = DecisionStore(tmp_path / "decisions.db")
    monkeypatch.setattr(STATE, "graph_config", config)
    monkeypatch.setattr(STATE, "telemetry_store", telemetry)
    monkeypatch.setattr(STATE, "decision_store", decisions)
    monkeypatch.setattr("graph.decisions._notify_created", lambda rec: None)
    app = FastAPI()
    register_decision_routes(app)
    register_telemetry_routes(app, spend_cap_status=turn_control.spend_cap_status)
    with TestClient(app) as client:
        yield config, telemetry, decisions, client


def record(store, *, origin="scheduler", cost=0, tokens=0, when=None):
    store.record({"task_id": "turn", "origin": origin, "cost_usd": cost, "total_tokens": tokens,
                  "ended_at": (when or datetime.now(UTC)).isoformat(), "state": "failed"})


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("cost,tokens", [(2, 0), (0, 100)])
async def test_both_drivers_pause_once_and_answer_resumes(cap, monkeypatch, tmp_path, streaming, cost, tokens):
    config, telemetry, decisions, client = cap
    scheduler = LocalScheduler(agent_name="test", invoke_url="http://unused", api_key="", bearer_token="",
                               db_dir=tmp_path / "scheduler")
    scheduler.add_job("enabled", "0 * * * *", job_id="on")
    scheduler.add_job("disabled", "0 * * * *", job_id="off")
    scheduler.set_enabled("off", False)
    monkeypatch.setattr(STATE, "scheduler", scheduler)
    record(telemetry, cost=cost, tokens=tokens)
    ran = []

    async def stream_impl(message, session_id, **kwargs):
        ran.append(session_id)
        yield ("text", "ran")

    async def sync_impl(message, session_id, **kwargs):
        ran.append(session_id)
        return [{"role": "assistant", "content": "ran"}]

    monkeypatch.setattr(chat, "_chat_langgraph_stream_impl", stream_impl)
    monkeypatch.setattr(chat._turn_sync, "_chat_langgraph_impl", sync_impl)
    monkeypatch.setattr(chat, "_note_agent_active", lambda sid: None)

    async def turn(origin, sid):
        if streaming:
            return [event async for event in chat._chat_langgraph_stream(
                "work", sid, request_metadata={"origin": origin})]
        return await chat._chat_langgraph("work", sid, origin=origin)

    # Default/off means no admission accounting or decisions, even above the limits.
    config.spend_cap_enabled = LangGraphConfig().spend_cap_enabled
    await turn("scheduler", "off")
    assert ran == ["off"] and decisions.list() == []
    config.spend_cap_enabled = True
    results = await asyncio.gather(turn("scheduler", "job-one"), turn("watch", "job-two"))
    assert all("spend cap" in str(result) for result in results)
    assert ran == ["off"]
    pending = decisions.list(state="pending")
    assert len(pending) == 1
    assert {job.id: job.enabled for job in scheduler.list_jobs()} == {"on": True, "off": False}
    monkeypatch.setattr(STATE, "decision_store", DecisionStore(decisions.path))
    await turn("webhook", "reattached")
    assert len(decisions.list()) == 1 and ran == ["off"]
    await turn("api-chat", "operator")
    assert ran == ["off", "operator"]
    status = client.get("/api/telemetry/spend-cap").json()
    assert status["paused"] and status["decision_id"] == pending[0].id
    assert client.post(f"/api/decisions/{pending[0].id}/cancel", json={}).status_code == 409
    response = client.post(f"/api/decisions/{pending[0].id}/answer", json={"option_id": "resume"})
    assert response.status_code == 200 and response.json()["resumed"]
    # Reattach to the file, as after restart: no process-local bypass is needed.
    monkeypatch.setattr(STATE, "decision_store", DecisionStore(decisions.path))
    await turn("inbox", "after-answer")
    assert ran == ["off", "operator", "after-answer"]
    assert not client.get("/api/telemetry/spend-cap").json()["paused"]
    assert len(decisions.list()) == 1
    assert {job.id: job.enabled for job in scheduler.list_jobs()} == {"on": True, "off": False}


def test_meter_counts_only_today_and_all_autonomous_origins(cap):
    _, telemetry, _, _ = cap
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    start = now.replace(hour=0)
    for origin in turn_control._AUTONOMOUS_ORIGINS:
        record(telemetry, origin=f" {origin.upper()} ", cost=1, tokens=10, when=start)
    for origin in ("", "local", "api-chat", "v1", "plugin", "unknown"):
        record(telemetry, origin=origin, cost=1000, tokens=10000, when=now)
    record(telemetry, cost=1000, tokens=10000, when=start - timedelta(microseconds=1000))
    record(telemetry, cost=1000, tokens=10000, when=start + timedelta(days=1))
    size = len(turn_control._AUTONOMOUS_ORIGINS)
    assert telemetry.daily_autonomous_spend(turn_control._AUTONOMOUS_ORIGINS, now=now) == {
        "day": "2026-11-18", "cost_usd": size, "total_tokens": size * 10,
    }


def test_pending_survives_midnight_and_answer_grants_only_current_day(cap, monkeypatch):
    _, telemetry, decisions, _ = cap
    day = [datetime(2026, 11, 18, 12, tzinfo=UTC)]
    meter = telemetry.daily_autonomous_spend
    monkeypatch.setattr(telemetry, "daily_autonomous_spend", lambda origins: meter(origins, now=day[0]))
    decisions._clock = lambda: day[0]
    record(telemetry, tokens=100, when=day[0])
    assert turn_control.autonomous_admission_error("scheduler", "job")
    rec = decisions.list()[0]
    day[0] += timedelta(days=1)
    assert turn_control.autonomous_admission_error("inbox", "other-job")
    assert len(decisions.list()) == 1
    decisions.answer(rec.id, "resume")
    record(telemetry, tokens=100, when=day[0])
    assert turn_control.autonomous_admission_error("scheduler", "job") is None
    day[0] += timedelta(days=1)
    assert turn_control.autonomous_admission_error("scheduler", "job") is None
    record(telemetry, tokens=100, when=day[0])
    assert turn_control.autonomous_admission_error("scheduler", "job")
    assert len(decisions.list()) == 2


@pytest.mark.parametrize("unavailable", ["missing", "read_failure"])
def test_unavailable_meter_never_admits_unattended_turn(cap, monkeypatch, unavailable):
    _, telemetry, _, _ = cap
    if unavailable == "missing":
        monkeypatch.setattr(STATE, "telemetry_store", None)
    else:
        def fail(origins):
            raise OSError("unreadable")
        monkeypatch.setattr(telemetry, "daily_autonomous_spend", fail)
    assert turn_control.autonomous_admission_error("scheduler", "job")
    assert turn_control.autonomous_admission_error("api-chat", "operator") is None


def test_spend_cap_config_round_trip():
    block = {"enabled": True, "daily_cost_hard_limit": 0.25, "daily_token_hard_limit": 500}
    config = LangGraphConfig.from_dict({"spend_cap": block}, _plugin_config={})
    assert config_to_dict(config)["spend_cap"] == block


@pytest.mark.asyncio
async def test_agent_cannot_withdraw_instance_pause(cap):
    from tools.lg_tools import withdraw_decision

    _, telemetry, decisions, _ = cap
    record(telemetry, tokens=100)
    assert turn_control.autonomous_admission_error("scheduler", "job")
    rec = decisions.list()[0]
    result = await withdraw_decision.ainvoke({"decision_id": rec.id, "state": {"session_id": "job"}})
    assert result.startswith("Error:")
    assert decisions.get(rec.id).pending


@pytest.mark.asyncio
@pytest.mark.parametrize("cron", [False, True])
@pytest.mark.parametrize("reconcile", [False, True])
async def test_refused_scheduled_fire_preserves_job_until_answer(cap, tmp_path, monkeypatch, cron, reconcile):
    import httpx
    from a2a.server.agent_execution import RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.server.events.event_queue import EventQueueLegacy
    from a2a.types import Message, Part, Role, SendMessageRequest
    from google.protobuf.json_format import MessageToDict

    from a2a_impl.executor import ProtoAgentExecutor

    record(cap[1], tokens=100)
    scheduler = LocalScheduler(agent_name="test", invoke_url="http://unused", api_key="", bearer_token="",
                               db_dir=tmp_path / "scheduler")
    due = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    job = scheduler.add_job("work", "* * * * *" if cron else due, max_fires=1 if cron else None,
                            ttl="1s" if cron else None)
    with scheduler._connect() as db:
        db.execute("UPDATE jobs SET next_fire = ?, created_at = ? WHERE id = ?", (due, due, job.id))
    messages, snapshots, ran = [], {}, []

    async def stream_impl(message, session_id, **kwargs):
        ran.append(session_id)
        yield ("text", "ran")

    monkeypatch.setattr(chat, "_chat_langgraph_stream_impl", stream_impl)
    monkeypatch.setattr(chat, "_note_agent_active", lambda sid: None)

    class Queue(EventQueueLegacy):
        async def enqueue_event(self, event):
            if hasattr(event, "status"):
                snapshots["status"] = MessageToDict(event.status)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, *, json, **kwargs):
            if json["method"] == "GetTask":
                payload = snapshots[json["params"]["id"]]
            else:
                wire = json["params"]["message"]
                messages.append(wire["messageId"])
                task_id = f"task-{len(messages)}"
                request = SendMessageRequest(message=Message(
                    message_id=wire["messageId"], role=Role.ROLE_USER, parts=[Part(text="go")],
                    metadata=wire["metadata"]))
                context = RequestContext(call_context=ServerCallContext(), request=request,
                                         task_id=task_id, context_id=wire["contextId"])
                await ProtoAgentExecutor(chat._chat_langgraph_stream,
                                         server_fired_origin=turn_control.is_autonomous_origin).execute(context, Queue())
                payload = {"result": {"task": {"id": task_id, "status": snapshots["status"]}}}
                snapshots[task_id] = payload
                if reconcile:
                    payload = {"result": {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}}
            return httpx.Response(200, json=payload, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    if reconcile:
        # An accepted task outlives the bounded wait; a later GetTask must apply
        # the same refusal disposition as an immediate terminal response.
        scheduler._fire_timeout_s = 0

    async def tick():
        await scheduler._tick()
        await asyncio.gather(*scheduler._fire_tasks)
        if reconcile:
            await scheduler._reconcile_occurrences()

    for _ in range(2):
        await tick()
        stored = scheduler.get_job(job.id)
        assert stored is not None and stored.enabled and stored.next_fire == due
        assert stored.fire_count == 0 and stored.consecutive_failures == 0
        assert stored.last_error is None and stored.last_ok is None
        scheduler._recover_missed_fires()  # A long pause must survive startup recovery.
        assert scheduler.get_job(job.id) is not None
    assert not ran and len(cap[2].list(state="pending")) == 1
    assert len(set(messages)) == 2  # Do not replay a terminal refused task on retry.
    decision = cap[2].list(state="pending")[0]
    assert cap[3].post(f"/api/decisions/{decision.id}/answer", json={"option_id": "resume"}).status_code == 200
    await tick()
    assert len(ran) == 1
    assert scheduler.get_job(job.id) is None  # Now the one-shot/max_fires is consumed.
