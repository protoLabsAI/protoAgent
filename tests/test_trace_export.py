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


def _partitioned_checkpoint(context_id, task_id, *, incognito=False):
    """Trusted-runner fixture: compact each audience independently before export."""
    from langgraph.checkpoint.base import empty_checkpoint

    from graph.compaction_engine import summary_message

    checkpoint = empty_checkpoint()
    private = [
        HumanMessage(content="PRIVATE_STEERING_CANARY"),
        AIMessage(content="PRIVATE_REPLY_CANARY", tool_calls=[
            {"name": "private_tool", "args": {"text": "PRIVATE_ARGUMENT_CANARY"}, "id": "private-call"},
        ]),
        ToolMessage(content="PRIVATE_RESULT_CANARY", tool_call_id="private-call"),
    ]
    public_messages = [summary_message("Public request summary"), *_MESSAGES]
    values = {
        "messages": public_messages,
        "orient": "Public plan",
        "tools": [{"type": "function", "function": {"name": "public_tool", "parameters": {}}}],
        "metadata": {
            **vars(_Outcome(context_id=context_id, task_id=task_id)),
            "teacher": "public-agent", "trace_id": "public-trace", "thinking": "on",
        },
    }
    checkpoint["channel_values"] = {
        "incognito": incognito,
        # The exporter must not assume these raw/legacy channels are safe, even
        # after a later public turn clears incognito and compacts its context.
        "messages": [summary_message("PRIVATE_COMPACTION_CANARY"), *private, *_MESSAGES],
        "audience_partitions": {"console": {"messages": private, "archive": private}},
        "fleet_export_projection": {
            "version": 1, "context_id": context_id, "task_id": task_id,
            **{key: {"audience": "public", "source_version": checkpoint["id"], "value": value}
               for key, value in values.items()},
        },
    }
    return checkpoint


@pytest.mark.parametrize("context_id", ["sess-1", "background:proof-1"])
def test_later_public_terminal_export_uses_only_public_bytes(tmp_path, monkeypatch, context_id):
    """Refs #5403: private turn → restart → public turn, including background IDs."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from runtime.state import STATE
    from server.a2a import _record_a2a_telemetry

    _enable(tmp_path, monkeypatch)
    # Poison every live auxiliary source, including the process-wide tools cache.
    monkeypatch.setenv("AGENT_NAME", "PRIVATE_TEACHER_CANARY")
    monkeypatch.setattr(trace_export, "_tool_schema_cache", [{"description": "PRIVATE_SCHEMA_CANARY"}])
    monkeypatch.setattr("observability.tracing.current_trace_id", lambda: "PRIVATE_TRACE_CANARY")

    class _PrivatePlan:
        def read_plan(self, sid):
            return "PRIVATE_PLAN_CANARY"

    monkeypatch.setattr("graph.goals.store.GoalStore", _PrivatePlan)
    monkeypatch.setattr("server.turn_telemetry.record_turn", lambda **kwargs: None)
    monkeypatch.setattr(STATE, "graph_config", SimpleNamespace(thinking="PRIVATE_CONFIG_CANARY"))
    monkeypatch.setattr(STATE, "graph", SimpleNamespace(bound_tools=[{"description": "PRIVATE_TOOL_CANARY"}]))
    outcome = SimpleNamespace(**{
        **vars(_Outcome(context_id=context_id)), "usage": {}, "incognito": True,
        "models": ["PRIVATE_MODEL_CANARY"], "trigger": "PRIVATE_TRIGGER_CANARY",
        "origin": "PRIVATE_ORIGIN_CANARY", "priority": "PRIVATE_PRIORITY_CANARY",
    })
    config = {"configurable": {"thread_id": f"a2a:{context_id}", "checkpoint_ns": ""}}
    db = str(tmp_path / "checkpoint.db")
    with SqliteSaver.from_conn_string(db) as saver:
        private = _partitioned_checkpoint(context_id, outcome.task_id, incognito=True)
        saver.put(config, private, {}, {})
        monkeypatch.setattr(STATE, "checkpointer", saver)
        _record_a2a_telemetry(outcome)
        assert not list(tmp_path.glob("*.jsonl"))

    # Reopen the persisted checkpoint: privacy must not depend on a process flag.
    with SqliteSaver.from_conn_string(db) as saver:
        later = _partitioned_checkpoint(context_id, outcome.task_id)
        retained = saver.get_tuple(config).checkpoint["channel_values"]["audience_partitions"]
        later["channel_values"]["audience_partitions"] = retained
        saver.put(config, later, {}, {})
        outcome.incognito = False
        monkeypatch.setattr(STATE, "checkpointer", saver)
        _record_a2a_telemetry(outcome)

    (row,) = _read_rows(tmp_path)
    exported = next(tmp_path.glob("*.jsonl")).read_bytes()
    assert b"PRIVATE_" not in exported
    assert b"Public request summary" in exported
    assert row["meta"]["orient"] == "Public plan"
    assert row["meta"]["session_id"] == context_id
    assert row["meta"]["model"] == "claude-opus-4-8"
    assert row["tools"][0]["function"]["name"] == "public_tool"
    assert row["teacher"] == "public-agent"
    assert row["id"] == "fleet__public-trace"


@pytest.mark.parametrize("component", ["messages", "orient", "tools", "metadata"])
@pytest.mark.parametrize("fault", ["missing", "missing_lineage", "mixed", "stale"])
def test_partitioned_export_refuses_unverified_component(tmp_path, monkeypatch, component, fault):
    _enable(tmp_path, monkeypatch)
    checkpoint = _partitioned_checkpoint("sess-1", "task-1")
    projection = checkpoint["channel_values"]["fleet_export_projection"]
    if fault == "missing":
        del projection[component]
    elif fault == "missing_lineage":
        del projection[component]["source_version"]
    elif fault == "mixed":
        projection[component]["audience"] = "mixed"
    else:
        projection[component]["source_version"] = "older-checkpoint"
    saver = SimpleNamespace(get_tuple=lambda config: SimpleNamespace(checkpoint=checkpoint))
    trace_export.export_turn(_Outcome(), checkpointer=saver, graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.parametrize("fault", ["missing", "malformed", "version", "context", "task", "read_failure"])
def test_partitioned_export_refuses_unknown_or_mismatched_snapshot(tmp_path, monkeypatch, fault, caplog):
    _enable(tmp_path, monkeypatch)
    checkpoint = _partitioned_checkpoint("sess-1", "task-1")
    channels = checkpoint["channel_values"]
    projection = channels["fleet_export_projection"]
    if fault == "missing":
        del channels["fleet_export_projection"]
    elif fault == "malformed":
        channels["fleet_export_projection"] = None
    elif fault == "version":
        projection["version"] = 99
    elif fault == "context":
        projection["context_id"] = "another-context"
    elif fault == "task":
        projection["task_id"] = "previous-task"

    def read(config):
        if fault == "read_failure":
            raise ValueError("PRIVATE_EXCEPTION_CANARY")
        return SimpleNamespace(checkpoint=checkpoint)

    trace_export.export_turn(_Outcome(), checkpointer=SimpleNamespace(get_tuple=read), graph_config=_Cfg())
    assert not list(tmp_path.glob("*.jsonl"))
    assert "PRIVATE_" not in caplog.text
