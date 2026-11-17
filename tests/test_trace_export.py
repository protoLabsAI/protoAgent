"""Tests for observability/trace_export.py — the fleet Observe seam (#1897).

Freezes the canonical Trajectory row shape the lab's ``dataset/adapters.py::_fleet``
consumes: OpenAI chat-format messages, verifiable reward from terminal state,
the OODA ``loop_shape``/``orient`` signal, incognito skip, and disabled no-op.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from observability import trace_export


@dataclass
class _Outcome:
    context_id: str = "sess-1"
    task_id: str = "task-1"
    state: str = "completed"
    models: list = field(default_factory=lambda: ["claude-opus-4-8"])
    origin: str = "scheduler"
    trigger: str = "job-9"
    priority: str = ""
    cost_usd: float = 0.02
    duration_ms: int = 1500
    llm_calls: int = 2
    tool_calls: int = 1


class _Checkpointer:
    """Minimal sync checkpointer stub mirroring ThreadedSqliteSaver.get_tuple."""

    def __init__(self, messages, incognito=False):
        self._messages = messages
        self._incognito = incognito

    def get_tuple(self, config):
        assert config["configurable"]["thread_id"] == "a2a:sess-1"

        class _Tup:
            checkpoint = {"channel_values": {"messages": self._messages, "incognito": self._incognito}}

        return _Tup()


@dataclass
class _Cfg:
    thinking: str = "enabled"


_MESSAGES = [
    SystemMessage(content="You are a helpful agent."),
    HumanMessage(content="What's the weather?"),
    AIMessage(
        content="Let me check.",
        tool_calls=[{"name": "get_weather", "args": {"city": "SF"}, "id": "call_1"}],
    ),
    ToolMessage(content="72F and sunny", tool_call_id="call_1"),
    AIMessage(content="It's 72F and sunny in SF."),
]


@pytest.fixture(autouse=True)
def _reset():
    trace_export._reset_for_test()
    yield
    trace_export._reset_for_test()


def _enable(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", str(tmp_path))
    trace_export.init()
    assert trace_export.is_enabled()


def _read_rows(tmp_path):
    files = list(tmp_path.glob("fleet-traces-*.jsonl"))
    assert len(files) == 1, f"expected one daily dump, got {files}"
    return [json.loads(line) for line in files[0].read_text().splitlines()]


def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", "off")
    trace_export.init()
    assert not trace_export.is_enabled()
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))


def test_completed_turn_row_shape(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg())

    (row,) = _read_rows(tmp_path)
    assert row["id"] == "fleet__task-1"  # no trace id in test → falls back to task id
    assert row["source"] == "protoagent-fleet"
    assert row["split"] == "train"
    assert row["verified"] is True
    assert row["reward"] == 1.0
    assert row["thinking"] == "on"

    roles = [m["role"] for m in row["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]

    # assistant tool_call → OpenAI shape, arguments as a JSON string
    call = row["messages"][2]["tool_calls"][0]
    assert call["name"] == "get_weather"
    assert call["id"] == "call_1"
    assert json.loads(call["arguments"]) == {"city": "SF"}
    # tool result carries the linking id
    assert row["messages"][3]["tool_call_id"] == "call_1"

    meta = row["meta"]
    assert meta["session_id"] == "sess-1"
    assert meta["origin"] == "scheduler"
    assert meta["loop_shape"] == "react"  # no goal plan on disk
    assert meta["outcome_state"] == "completed"


@pytest.mark.parametrize(
    "state,verified,reward",
    [("completed", True, 1.0), ("failed", True, 0.0), ("canceled", False, None)],
)
def test_reward_mapping(tmp_path, monkeypatch, state, verified, reward):
    _enable(tmp_path, monkeypatch)
    trace_export.export_turn(_Outcome(state=state), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg())
    (row,) = _read_rows(tmp_path)
    assert row["verified"] is verified
    assert row["reward"] == reward


def test_incognito_thread_skipped(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer(_MESSAGES, incognito=True), graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))


def test_empty_transcript_skipped(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer([]), graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))


def test_ooda_label_from_goal_plan(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)

    class _Plan:
        def read_plan(self, sid):
            return "## Goal\n- [ ] step one\n- [x] step two"

    import graph.goals.store as store_mod

    monkeypatch.setattr(store_mod, "GoalStore", lambda *a, **k: _Plan())
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg())

    (row,) = _read_rows(tmp_path)
    assert row["meta"]["loop_shape"] == "ooda"
    assert "step one" in row["meta"]["orient"]


def test_config_toggle_enables_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PROTOAGENT_FLEET_TRACE_EXPORT", raising=False)

    class _Paths:
        def store(self, name):
            return tmp_path / name

    import infra.paths

    monkeypatch.setattr(infra.paths, "instance_paths", lambda: _Paths())
    trace_export.init(config_enabled=True)
    assert trace_export.is_enabled()
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg())
    files = list((tmp_path / "fleet-traces").glob("*.jsonl"))
    assert len(files) == 1


def test_env_off_overrides_config_on(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", "0")
    trace_export.init(config_enabled=True)  # env off wins
    assert not trace_export.is_enabled()


def test_env_path_wins_over_config(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_FLEET_TRACE_EXPORT", str(tmp_path))
    trace_export.init(config_enabled=False)  # env path enables regardless of config
    assert trace_export.is_enabled()


def test_disabled_when_neither_env_nor_config(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_FLEET_TRACE_EXPORT", raising=False)
    trace_export.init(config_enabled=False)
    assert not trace_export.is_enabled()


def test_export_never_raises_on_bad_checkpointer(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)

    class _Boom:
        def get_tuple(self, config):
            raise RuntimeError("db gone")

    # Must not propagate — export is best-effort.
    trace_export.export_turn(_Outcome(), checkpointer=_Boom(), graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))


def _public_source(messages=_MESSAGES):
    from graph.public_memory import FleetExport, PublicSource

    return PublicSource("retained-version-2", tuple(messages), fleet=FleetExport(
        task_id="task-1", orient="Public plan",
        tools=[{"type": "function", "function": {"name": "public_tool", "parameters": {}}}],
        metadata={**vars(_Outcome()), "teacher": "public-agent", "trace_id": "public-trace", "thinking": "on"},
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("context_id", ["sess-1", "background:proof-1"])
async def test_later_public_terminal_export_uses_only_public_bytes(tmp_path, monkeypatch, context_id):
    """Real private task storage survives restart and a later compacted public turn."""
    from a2a.server.context import ServerCallContext
    from a2a.types import a2a_pb2
    from langchain_core.messages import messages_from_dict, messages_to_dict

    from a2a_impl.stores import ReasoningCoalescingTaskStore, make_sqlite_engine
    from graph import public_memory
    from graph.compaction_engine import summarize_span, summary_message
    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    _enable(tmp_path, monkeypatch)
    operator = ServerCallContext(state={"principal_kind": "device"})
    db = str(tmp_path / "tasks.db")
    engine = make_sqlite_engine(db)
    store = ReasoningCoalescingTaskStore(engine)
    task = a2a_pb2.Task(id="task-1", context_id=context_id, history=[
        a2a_pb2.Message(role=a2a_pb2.ROLE_USER, parts=[a2a_pb2.Part(text="Public request")]),
    ])
    private = [HumanMessage(content="PRIVATE_STEERING_CANARY"), AIMessage(
        content="PRIVATE_REPLY_CANARY", tool_calls=[
            {"name": "private_tool", "args": {"text": "PRIVATE_ARGUMENT_CANARY"}, "id": "private-call"},
        ]), ToolMessage(content="PRIVATE_RESULT_CANARY", tool_call_id="private-call")]
    # Actually run the engine: its output retains the operator's private canary.
    private_summary = summarize_span(private, lambda prompt: "PRIVATE_COMPACTION_CANARY")
    assert "PRIVATE_STEERING_CANARY" in private_summary
    await store.save_private(task, {"messages": messages_to_dict(private), "summary": private_summary}, operator)
    await engine.dispose()

    engine = make_sqlite_engine(db)
    store = ReasoningCoalescingTaskStore(engine)
    try:
        record = await store.get_record(task.id, operator)
        retained_private = messages_from_dict(record.private_payload["messages"])
        assert retained_private[0].content == "PRIVATE_STEERING_CANARY"
        public_messages = [HumanMessage(content=m.parts[0].text) for m in record.task.history]
        prompts = []

        def summarize(prompt):
            prompts.append(prompt)
            return "Public request summary"

        compacted = summary_message(summarize_span(public_messages, summarize))
        assert prompts and all("PRIVATE_" not in prompt for prompt in prompts)
        task.history.append(a2a_pb2.Message(role=a2a_pb2.ROLE_AGENT, parts=[a2a_pb2.Part(text="Public reply")]))
        await store.save(task, operator)
        assert (await store.get_record(task.id, operator)).private_payload["summary"] == private_summary
        # Test-only trusted adapter: public SDK history and independently compacted
        # public summary become the shared reader's atomic public source.
        source = _public_source([compacted, *_MESSAGES])
        monkeypatch.setattr(public_memory, "_source_reader", lambda sid: source)
        monkeypatch.setenv("AGENT_NAME", "PRIVATE_TEACHER_CANARY")
        monkeypatch.setattr(trace_export, "_tool_schema_cache", [{"description": "PRIVATE_SCHEMA_CANARY"}])
        monkeypatch.setattr("observability.tracing.current_trace_id", lambda: "PRIVATE_TRACE_CANARY")
        monkeypatch.setattr("graph.goals.store.GoalStore", lambda: SimpleNamespace(read_plan=lambda sid: "PRIVATE_PLAN_CANARY"))
        monkeypatch.setattr("server.turn_telemetry.record_turn", lambda **kwargs: None)
        monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(thinking="PRIVATE_CONFIG_CANARY"))
        monkeypatch.setattr(STATE, "graph", SimpleNamespace(bound_tools=[]))
        outcome = SimpleNamespace(**{**vars(_Outcome(context_id=context_id)), "usage": {}, "incognito": True,
                                    "models": ["PRIVATE_MODEL_CANARY"], "trigger": "PRIVATE_TRIGGER_CANARY"})
        checkpoint = {"channel_values": {"incognito": True, "messages": retained_private}}
        monkeypatch.setattr(STATE, "checkpointer", SimpleNamespace(get_tuple=lambda config: SimpleNamespace(checkpoint=checkpoint)))
        _record_a2a_telemetry(outcome)
        assert not list(tmp_path.glob("*.jsonl"))
        checkpoint["channel_values"].update(incognito=False, messages=[summary_message(private_summary), *_MESSAGES])
        outcome.incognito = False
        _record_a2a_telemetry(outcome)
    finally:
        await engine.dispose()

    (row,) = _read_rows(tmp_path)
    exported = next(tmp_path.glob("*.jsonl")).read_bytes()
    assert b"PRIVATE_" not in exported
    assert b"Public request summary" in exported
    assert row["meta"]["session_id"] == context_id
    assert row["meta"]["orient"] == "Public plan"
    assert row["meta"]["model"] == "claude-opus-4-8"
    assert row["meta"]["cost_usd"] == 0.02
    assert row["tools"][0]["function"]["name"] == "public_tool"
    assert row["teacher"] == "public-agent"
    assert row["id"] == "fleet__public-trace"


@pytest.mark.parametrize("fault", ["missing", "mixed", "version", "auxiliary", "metadata", "task", "stale"])
def test_partitioned_export_refuses_unverified_source(tmp_path, monkeypatch, fault, caplog):
    from dataclasses import replace

    from graph import public_memory

    _enable(tmp_path, monkeypatch)
    source = _public_source()
    if fault == "mixed":
        source = replace(source, audience="mixed")
    elif fault == "version":
        source = replace(source, version="")
    elif fault == "auxiliary":
        source = replace(source, fleet=None)
    elif fault == "metadata":
        del source.fleet.metadata["models"]
    elif fault == "task":
        source = replace(source, fleet=replace(source.fleet, task_id="previous-task"))

    def read(sid):
        if fault in {"missing", "stale"}:
            # The shared contract makes unavailable/stale authoritative data an
            # error, never None (which means known unpartitioned).
            raise ValueError("PRIVATE_SOURCE_FAILURE_CANARY")
        return source

    monkeypatch.setattr(public_memory, "_source_reader", read)
    trace_export.export_turn(_Outcome(), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))
    assert "PRIVATE_" not in caplog.text
