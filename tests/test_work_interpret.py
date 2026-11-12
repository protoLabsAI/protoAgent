"""Marked chat interpretation: invocation boundary, wire contract and turn isolation."""

import asyncio
import importlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from pydantic import ValidationError

from server import work_interpret as wi


VALID = {
    "says": "Review the change",
    "kind": "job",
    "group": "doing",
    "job": {"what": "Review the change", "who": "agent"},
    "problem": None,
}


@pytest.fixture
def runtime(monkeypatch):
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=SimpleNamespace(values={"messages": [HumanMessage(content="Our project is Atlas")]}, next=())
        ),
        aupdate_state=AsyncMock(),
    )
    for name, value in {
        "graph": graph,
        "graph_config": SimpleNamespace(),
        "scheduler": None,
        "watch_controller": None,
        "delegate_registry": None,
        "plugin_tools": [],
        "mcp_tools": [],
    }.items():
        monkeypatch.setattr(wi.STATE, name, value)
    monkeypatch.setattr(wi, "plugin_verifier_names", lambda: [])
    return graph


@pytest.mark.asyncio
async def test_marked_turn_reads_context_denies_mutations_and_returns_one_reply(monkeypatch, runtime):
    writes = []

    @tool
    async def read_file() -> str:
        """Read project context."""
        return "Atlas read result"

    @tool
    async def schedule_task() -> str:
        """A forbidden writer."""
        writes.append("schedule")
        return "created"

    monkeypatch.setattr(wi, "get_all_tools", lambda **kw: [read_file, schedule_task])
    names = [
        "read_file",
        "schedule_task",
        "create_watch",
        "record_directive",
        "task_create",
        "execute_code",
        "run_command",
        "delegate_to",
        "ask_human",
        "unknown_plugin_write",
    ]
    responses = [
        AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": str(i)} for i, name in enumerate(names)]),
        AIMessage(content=json.dumps(VALID)),
    ]
    seen = []

    class Model:
        def bind_tools(self, tools):
            assert [t.name for t in tools] == ["read_file"]
            return self

        async def ainvoke(self, messages):
            seen.append(list(messages))
            return responses.pop(0)

    monkeypatch.setattr(wi, "create_llm", lambda *a, **kw: Model())
    chat = importlib.import_module("server.chat")
    normal = AsyncMock(side_effect=AssertionError("ordinary turn middleware must not run"))
    monkeypatch.setattr(chat, "_chat_langgraph", normal)
    result = await chat.chat("Review it", "chat-interpret", interpret=True)
    assert len(result) == 1
    assert result[0]["role"] == "system"
    assert wi.Interpretation.model_validate_json(result[0]["content"]).job.what == "Review the change"
    assert any(m.content == "Our project is Atlas" for m in seen[0])
    assert seen[1][-10].content == "Atlas read result"
    assert all("Denied" in m.content for m in seen[1][-9:])
    assert not writes
    normal.assert_not_awaited()
    record = runtime.aupdate_state.call_args.args[1]["messages"][0]
    assert isinstance(record, SystemMessage)
    assert record.additional_kwargs == {"interpret": True, "system_work": True, "folded": True}


@pytest.mark.asyncio
async def test_concurrent_and_subsequent_normal_turns_keep_permissions(monkeypatch, runtime):
    chat = importlib.import_module("server.chat")
    entered, release = asyncio.Event(), asyncio.Event()

    async def marked(*a, **kw):
        entered.set()
        await release.wait()
        return []

    monkeypatch.setattr(wi, "interpret_turn", marked)
    ordinary = AsyncMock(return_value=[{"role": "assistant", "content": "normal"}])
    monkeypatch.setattr(chat, "_chat_langgraph", ordinary)
    pending = asyncio.create_task(chat.chat("interpret", "chat-one", interpret=True))
    await entered.wait()
    await chat.chat("ordinary", "chat-two")
    release.set()
    await pending
    await chat.chat("ordinary", "chat-one")
    assert ordinary.await_count == 2
    assert all(call.kwargs["tool_fence"] is None for call in ordinary.call_args_list)


def test_unknown_watch_is_a_problem_not_a_fabricated_verifier(runtime):
    raw = {
        "says": "Watch Slack",
        "kind": "followup",
        "group": "coming",
        "followup": {
            "trigger": "condition",
            "condition": {
                "condition": "Slack message arrives",
                "check": "slack:messages",
                "check_args": {},
                "interval_s": 60,
                "repeat": True,
                "expires_in_s": None,
            },
            "then": "Tell me",
        },
        "problem": None,
    }
    reply = wi.validate_reply(json.dumps(raw))
    assert reply.problem
    assert reply.followup is None


@pytest.mark.parametrize(
    "change",
    [{"group": "coming"}, {"job": None}, {"extra": 1}, {"instruction": {"quote": "a", "scope": "b", "conditions": ""}}],
)
def test_invalid_contract_rejected(change):
    with pytest.raises(ValidationError):
        wi.Interpretation.model_validate({**VALID, **change})


def test_schedules_and_delegate_capabilities(runtime, monkeypatch):
    with pytest.raises(ValueError):
        wi.Time(first_run="2026-11-12", repeat="once", repeat_label="Once", timezone="UTC")
    with pytest.raises(ValueError):
        wi.Time(first_run="2026-11-12T12:00:00+00:00", repeat="not cron", repeat_label="Daily", timezone="UTC")
    text = json.dumps({**VALID, "job": {"what": "Review", "who": "delegate"}})
    assert wi.validate_reply(text).problem
    monkeypatch.setattr(wi.STATE, "delegate_registry", SimpleNamespace(names=lambda: ["coder"]))
    assert wi.validate_reply(text).problem is None


@pytest.mark.asyncio
async def test_bad_model_output_and_parked_checkpoint(monkeypatch, runtime):
    runtime.aget_state.return_value.next = ("tools",)
    monkeypatch.setattr(wi, "read_tools", lambda fence: {})
    monkeypatch.setattr(
        wi,
        "create_llm",
        lambda *a, **kw: SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="not JSON"))),
    )
    result = await wi.interpret_turn("hello", "chat-parked")
    assert len(result) == 1
    assert wi.Interpretation.model_validate_json(result[0]["content"]).problem
    runtime.aupdate_state.assert_not_awaited()


def test_route_passes_explicit_marker(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import operator_api.chat_routes as routes

    fake = AsyncMock(return_value=[{"role": "system", "content": json.dumps(VALID), "metadata": {"interpret": True}}])
    monkeypatch.setattr(routes, "chat", fake)
    app = FastAPI()
    routes.register_chat_routes(app, ui="none")
    with TestClient(app) as client:
        response = client.post("/api/chat", json={"message": "review", "session_id": "chat-route", "interpret": True})
    assert response.status_code == 200
    assert fake.call_args.kwargs["interpret"] is True
    assert response.json()["messages"][0]["role"] == "system"
    assert json.loads(response.json()["response"]) == VALID


@pytest.mark.asyncio
async def test_native_filesystem_reads_and_mutation_exclusion(monkeypatch, runtime, tmp_path):
    from graph.config import LangGraphConfig

    source = tmp_path / "context.txt"
    source.write_text("Atlas project context")
    config = LangGraphConfig()
    config.filesystem_enabled = True
    config.projects = [{"name": "atlas", "path": str(tmp_path), "write": True}]
    monkeypatch.setattr(wi.STATE, "graph_config", config)
    allowed = wi.read_tools()
    assert "read_file" in allowed
    assert "write_file" not in allowed
    assert "run_command" not in allowed
    result = await wi.invoke_read(
        {"name": "read_file", "args": {"project": "atlas", "path": "context.txt"}, "id": "read", "type": "tool_call"},
        allowed,
    )
    assert "Atlas project context" in result.content
    await wi.invoke_read(
        {
            "name": "write_file",
            "args": {"project": "atlas", "path": "context.txt", "content": "changed"},
            "id": "write",
            "type": "tool_call",
        },
        allowed,
    )
    assert source.read_text() == "Atlas project context"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(wi.DENIED))
async def test_control_tools_cannot_opt_in_as_reads(name):
    writer = SimpleNamespace(name=name, metadata={"access": "read"}, ainvoke=AsyncMock())
    result = await wi.invoke_read({"name": name, "id": "bad", "args": {}}, {name: writer})
    assert "Denied" in result.content
    writer.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_parked_calls_are_only_repaired_in_model_input(monkeypatch, runtime):
    pending = AIMessage(content="Waiting", tool_calls=[{"name": "ask_human", "args": {}, "id": "pending"}])
    runtime.aget_state.return_value.values["messages"].append(pending)
    runtime.aget_state.return_value.next = ("tools",)
    seen = []

    async def respond(messages):
        seen.extend(messages)
        return AIMessage(content=json.dumps(VALID))

    monkeypatch.setattr(wi, "read_tools", lambda fence: {})
    monkeypatch.setattr(wi, "create_llm", lambda *a, **kw: SimpleNamespace(ainvoke=respond))
    await wi.interpret_turn("Review", "chat-pending")
    assert pending.tool_calls  # durable input is intact
    assert not any(getattr(m, "tool_calls", []) for m in seen)
    runtime.aupdate_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_detached_interpretation_retains_structured_system_reply(monkeypatch, runtime):
    from runtime import local_turns

    monkeypatch.setattr(wi, "read_tools", lambda fence: {})
    monkeypatch.setattr(
        wi,
        "create_llm",
        lambda *a, **kw: SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content=json.dumps(VALID)))),
    )
    chat = importlib.import_module("server.chat")
    turn_id = local_turns.start_detached(
        chat.chat("Review", "chat-detached-interpret", interpret=True),
        session_id="chat-detached-interpret",
        origin="api-chat",
        error_adapter=chat.turn_error,
    )
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            outcome = local_turns._FINISHED.get(turn_id)
            if outcome:
                break
        assert outcome.state == "completed"
        assert outcome.messages[0]["metadata"]["interpret"]
        assert outcome.messages[0]["role"] == "system"
    finally:
        local_turns._FINISHED.pop(turn_id, None)


@pytest.mark.parametrize("trigger", ["time", "condition"])
def test_supported_followups_validate_without_creating_work(monkeypatch, runtime, trigger):
    scheduler = SimpleNamespace(add_job=AsyncMock())
    watches = SimpleNamespace(create=AsyncMock())
    monkeypatch.setattr(wi.STATE, "scheduler", scheduler)
    monkeypatch.setattr(wi.STATE, "watch_controller", watches)
    monkeypatch.setattr(wi, "plugin_verifier_names", lambda: ["github:checks_green"])
    followup = {"trigger": trigger, "then": "Review the result"}
    if trigger == "time":
        followup["time"] = {
            "first_run": "2030-01-01T09:00:00+00:00",
            "repeat": "0 9 * * *",
            "repeat_label": "Daily",
            "timezone": "UTC",
        }
    else:
        followup["condition"] = {
            "condition": "Checks pass",
            "check": "github:checks_green",
            "check_args": {"pr": 12},
            "interval_s": 60,
            "repeat": False,
            "expires_in_s": None,
        }
    reply = wi.validate_reply(
        json.dumps({"says": "Follow up", "kind": "followup", "group": "coming", "followup": followup, "problem": None})
    )
    assert reply.problem is None
    assert reply.followup.trigger == trigger
    scheduler.add_job.assert_not_called()
    watches.create.assert_not_called()


def test_instruction_contract_keeps_quote_scope_and_conditions(runtime):
    instruction = {"quote": "Always test changes", "scope": "This project", "conditions": "Before delivery"}
    reply = wi.validate_reply(
        json.dumps(
            {
                "says": "Test before delivery",
                "kind": "instruction",
                "group": "told",
                "instruction": instruction,
                "problem": None,
            }
        )
    )
    assert reply.instruction.model_dump() == instruction
    assert reply.job is None and reply.followup is None
