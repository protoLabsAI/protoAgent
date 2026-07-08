"""Tests for observability/trace_export.py — the fleet Observe seam (#1897).

Freezes the canonical Trajectory row shape the lab's ``dataset/adapters.py::_fleet``
consumes: OpenAI chat-format messages, verifiable reward from terminal state,
the OODA ``loop_shape``/``orient`` signal, incognito skip, and disabled no-op.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

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
    trace_export.export_turn(
        _Outcome(state=state), checkpointer=_Checkpointer(_MESSAGES), graph_config=_Cfg()
    )
    (row,) = _read_rows(tmp_path)
    assert row["verified"] is verified
    assert row["reward"] == reward


def test_incognito_thread_skipped(tmp_path, monkeypatch):
    _enable(tmp_path, monkeypatch)
    trace_export.export_turn(
        _Outcome(), checkpointer=_Checkpointer(_MESSAGES, incognito=True), graph_config=_Cfg()
    )
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
