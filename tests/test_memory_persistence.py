"""Unit tests for graph.middleware.memory._persist_session.

Covers:
- successful file persistence with correct JSON structure
- atomic write resilience (temp file cleanup, no partial writes visible)
- opt-out when PROTOAGENT_DISABLE_MEMORY=1
- custom MEMORY_PATH override
- automatic directory creation
- graceful handling of permission errors
- tool_calls count and top-5 duration sorting
- empty session handling
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reload_memory(env_overrides: dict | None = None):
    """Reload graph.middleware.memory with given env overrides active.

    Returns the freshly-imported module so tests can call _persist_session
    with a known MEMORY_PATH / PROTOAGENT_DISABLE_MEMORY state.
    """
    env_overrides = env_overrides or {}
    with patch.dict(os.environ, env_overrides, clear=False):
        if "graph.middleware.memory" in sys.modules:
            del sys.modules["graph.middleware.memory"]
        import graph.middleware.memory as mod
        return mod


def _make_state(
    session_id: str = "test-session-1",
    messages=None,
):
    """Build a minimal state dict as the middleware would see it."""
    if messages is None:
        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there! How can I help you today? " * 5),
        ]
    return {
        "session_id": session_id,
        "messages": messages,
        "context": "",
        "captured_messages": [],
    }


# ---------------------------------------------------------------------------
# 1. Successful persistence with correct JSON structure
# ---------------------------------------------------------------------------

def test_persist_session_creates_json_file(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = _make_state("abc-123")
    with patch("observability.tracing.current_trace_id", return_value="trace-xyz"):
        mod._persist_session(state, "trace-xyz")

    expected = tmp_path / "abc-123.json"
    assert expected.exists(), "session JSON file was not created"

    data = json.loads(expected.read_text())
    assert data["session_id"] == "abc-123"
    assert data["trace_id"] == "trace-xyz"
    assert isinstance(data["messages"], list)
    assert isinstance(data["tool_calls"], list)
    assert "timestamp" in data


def test_persist_session_json_has_required_fields(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = _make_state("req-fields")
    with patch("observability.tracing.current_trace_id", return_value="t1"):
        mod._persist_session(state, "t1")

    data = json.loads((tmp_path / "req-fields.json").read_text())
    for key in ("session_id", "trace_id", "messages", "tool_calls", "final_output", "timestamp"):
        assert key in data, f"missing required field: {key}"


def test_persist_session_captures_messages(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    messages = [
        HumanMessage(content="What is 2+2?"),
        AIMessage(content="The answer is 4."),
    ]
    state = _make_state("msg-test", messages=messages)
    mod._persist_session(state, "t2")

    data = json.loads((tmp_path / "msg-test.json").read_text())
    roles = [m["role"] for m in data["messages"]]
    assert "user" in roles
    assert "assistant" in roles
    contents = [m["content"] for m in data["messages"]]
    assert any("2+2" in c for c in contents)
    assert any("4" in c for c in contents)


def test_persist_session_strips_reasoning_from_assistant(tmp_path):
    """ADR 0021: the persisted session (later injected as <prior_sessions>) must
    not carry the model's <scratch_pad> — strip it at the write source."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})
    messages = [
        HumanMessage(content="What is 2+2?"),
        AIMessage(content="<scratch_pad>add 2 and 2, that's 4</scratch_pad><output>It's 4.</output>"),
    ]
    state = _make_state("strip-test", messages=messages)
    mod._persist_session(state, "t")

    data = json.loads((tmp_path / "strip-test.json").read_text())
    blob = json.dumps(data)
    assert "scratch_pad" not in blob
    assert "add 2 and 2" not in blob          # internal reasoning gone
    assert "It's 4." in blob                   # user-facing answer kept
    assert "scratch_pad" not in (data["final_output"] or "")


def test_persist_session_final_output_is_last_ai_message(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    messages = [
        HumanMessage(content="Hi"),
        AIMessage(content="First reply."),
        HumanMessage(content="Tell me more"),
        AIMessage(content="This is the final answer."),
    ]
    state = _make_state("final-out", messages=messages)
    mod._persist_session(state, "t3")

    data = json.loads((tmp_path / "final-out.json").read_text())
    assert data["final_output"] == "This is the final answer."


# ---------------------------------------------------------------------------
# 2. Atomic write — no partial file visible
# ---------------------------------------------------------------------------

def test_atomic_write_no_partial_file_on_error(tmp_path):
    """If json.dump fails mid-write, no corrupted file should exist at dest."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = _make_state("atomic-test")
    dest = tmp_path / "atomic-test.json"


    def bad_dump(*args, **kwargs):
        raise OSError("simulated write error")

    with patch("json.dump", side_effect=bad_dump):
        mod._persist_session(state, "t4")

    # Destination file must NOT exist — no partial write
    assert not dest.exists(), "partial file should not be visible on write failure"

    # No .tmp files left behind
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0, f"temp files not cleaned up: {tmp_files}"


def test_atomic_write_succeeds_with_valid_rename(tmp_path):
    """After a successful write, only the final .json file exists."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = _make_state("rename-test")
    mod._persist_session(state, "t5")

    assert (tmp_path / "rename-test.json").exists()
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert len(tmp_files) == 0, "temp files should be cleaned up after successful write"


# ---------------------------------------------------------------------------
# 3. Opt-out via PROTOAGENT_DISABLE_MEMORY
# ---------------------------------------------------------------------------

def test_disabled_by_env_var(tmp_path):
    mod = _reload_memory({
        "MEMORY_PATH": str(tmp_path),
        "PROTOAGENT_DISABLE_MEMORY": "1",
    })

    state = _make_state("disabled-session")
    mod._persist_session(state, "t6")

    # No file should be created
    assert not list(tmp_path.glob("*.json")), "no file should be written when disabled"


@pytest.mark.parametrize("value", ["1", "true", "True"])
def test_disabled_by_env_var_variants(tmp_path, value):
    mod = _reload_memory({
        "MEMORY_PATH": str(tmp_path),
        "PROTOAGENT_DISABLE_MEMORY": value,
    })

    state = _make_state("disabled-variant")
    mod._persist_session(state, "t7")

    assert not list(tmp_path.glob("*.json")), f"no file should be written for PROTOAGENT_DISABLE_MEMORY={value!r}"


# ---------------------------------------------------------------------------
# 4. Custom MEMORY_PATH override
# ---------------------------------------------------------------------------

def test_custom_memory_path(tmp_path):
    custom = tmp_path / "custom" / "path"
    mod = _reload_memory({
        "MEMORY_PATH": str(custom),
        "PROTOAGENT_DISABLE_MEMORY": "",
    })

    state = _make_state("custom-path-session")
    mod._persist_session(state, "t8")

    expected = custom / "custom-path-session.json"
    assert expected.exists(), f"file not written to custom path: {expected}"
    data = json.loads(expected.read_text())
    assert data["session_id"] == "custom-path-session"


# ---------------------------------------------------------------------------
# 5. Automatic directory creation
# ---------------------------------------------------------------------------

def test_directory_auto_created(tmp_path):
    nested = tmp_path / "a" / "b" / "c"
    assert not nested.exists(), "pre-condition: directory should not exist yet"

    mod = _reload_memory({
        "MEMORY_PATH": str(nested),
        "PROTOAGENT_DISABLE_MEMORY": "",
    })

    state = _make_state("mkdir-session")
    mod._persist_session(state, "t9")

    assert nested.exists(), "directory should have been created automatically"
    assert (nested / "mkdir-session.json").exists()


# ---------------------------------------------------------------------------
# 6. Graceful permission error handling
# ---------------------------------------------------------------------------

def test_permission_error_does_not_raise(tmp_path):
    """If makedirs raises PermissionError, persist_session must not raise."""
    mod = _reload_memory({
        "MEMORY_PATH": str(tmp_path / "no-perms"),
        "PROTOAGENT_DISABLE_MEMORY": "",
    })

    state = _make_state("perm-session")

    with patch("os.makedirs", side_effect=OSError("Permission denied")):
        # Must not raise
        mod._persist_session(state, "t10")


def test_write_error_does_not_raise(tmp_path):
    """If the write itself fails (disk full etc.), persist_session must not raise."""
    mod = _reload_memory({
        "MEMORY_PATH": str(tmp_path),
        "PROTOAGENT_DISABLE_MEMORY": "",
    })

    state = _make_state("write-err-session")

    with patch("os.rename", side_effect=OSError("No space left on device")):
        # Must not raise
        mod._persist_session(state, "t11")


# ---------------------------------------------------------------------------
# 7. Tool call count and top-5 by duration sorting
# ---------------------------------------------------------------------------

def _ai_msg_with_tool_calls(tool_calls: list[dict]) -> AIMessage:
    """Build an AIMessage that carries tool_calls metadata."""
    msg = AIMessage(content="")
    msg.tool_calls = tool_calls
    return msg


def _tool_msg(tool_call_id: str, content: str) -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=tool_call_id)


def test_tool_calls_included_in_summary(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    tc1 = {"id": "tc1", "name": "search", "args": {"query": "python"}}
    tc2 = {"id": "tc2", "name": "calculator", "args": {"expression": "1+1"}}
    messages = [
        HumanMessage(content="Do something"),
        _ai_msg_with_tool_calls([tc1, tc2]),
        _tool_msg("tc1", "search result"),
        _tool_msg("tc2", "2"),
        AIMessage(content="Done."),
    ]
    state = _make_state("tool-test", messages=messages)
    mod._persist_session(state, "t12")

    data = json.loads((tmp_path / "tool-test.json").read_text())
    assert len(data["tool_calls"]) == 2
    names = {tc["name"] for tc in data["tool_calls"]}
    assert names == {"search", "calculator"}


def test_tool_calls_total_count_when_more_than_5(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    # 7 tool calls
    tcs = [{"id": f"tc{i}", "name": f"tool_{i}", "args": {}} for i in range(7)]
    tool_msgs = [_tool_msg(f"tc{i}", f"result {i}") for i in range(7)]
    messages = [
        HumanMessage(content="Do things"),
        _ai_msg_with_tool_calls(tcs),
        *tool_msgs,
        AIMessage(content="All done."),
    ]
    state = _make_state("many-tools", messages=messages)
    mod._persist_session(state, "t13")

    data = json.loads((tmp_path / "many-tools.json").read_text())
    assert len(data["tool_calls"]) == 5
    assert data["tool_calls_total_count"] == 7


def test_tool_calls_no_total_count_when_5_or_fewer(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    tcs = [{"id": f"tc{i}", "name": f"tool_{i}", "args": {}} for i in range(3)]
    tool_msgs = [_tool_msg(f"tc{i}", f"result {i}") for i in range(3)]
    messages = [
        HumanMessage(content="Do 3 things"),
        _ai_msg_with_tool_calls(tcs),
        *tool_msgs,
        AIMessage(content="Done."),
    ]
    state = _make_state("few-tools", messages=messages)
    mod._persist_session(state, "t14")

    data = json.loads((tmp_path / "few-tools.json").read_text())
    assert len(data["tool_calls"]) == 3
    assert "tool_calls_total_count" not in data


# ---------------------------------------------------------------------------
# 8. Empty session
# ---------------------------------------------------------------------------

def test_empty_session_creates_file_with_empty_arrays(tmp_path):
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = {
        "session_id": "empty-session",
        "messages": [],
        "context": "",
        "captured_messages": [],
    }
    mod._persist_session(state, "t15")

    expected = tmp_path / "empty-session.json"
    assert expected.exists(), "file should be created even for empty session"

    data = json.loads(expected.read_text())
    assert data["session_id"] == "empty-session"
    assert data["messages"] == []
    assert data["tool_calls"] == []
    assert data["final_output"] is None
    assert "timestamp" in data


def test_persist_session_falls_back_to_contextvar_session_id(tmp_path):
    """When state has no session_id (LangGraph drops the undeclared key),
    the summary is keyed by tracing.current_session_id() instead of pooling
    every session into unknown.json."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = {"session_id": "", "messages": [], "context": "", "captured_messages": []}
    with patch("observability.tracing.current_session_id", return_value="ctx-sid-42"):
        mod._persist_session(state, "t-ctx")

    assert (tmp_path / "ctx-sid-42.json").exists()
    assert not (tmp_path / "unknown.json").exists()
    data = json.loads((tmp_path / "ctx-sid-42.json").read_text())
    assert data["session_id"] == "ctx-sid-42"


def test_persist_session_unknown_only_when_no_session_id_anywhere(tmp_path):
    """With neither a state session_id nor a contextvar value, fall back to
    the historical unknown.json so persistence still happens."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    state = {"session_id": "", "messages": [], "context": "", "captured_messages": []}
    with patch("observability.tracing.current_session_id", return_value=""):
        mod._persist_session(state, "t-none")

    assert (tmp_path / "unknown.json").exists()


# ---------------------------------------------------------------------------
# 9. on_session_end middleware hook
# ---------------------------------------------------------------------------

def test_on_session_end_calls_persist_session(tmp_path):
    """MemoryMiddleware.on_session_end must call _persist_session."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    store = MagicMock()
    mw = mod.MemoryMiddleware(knowledge_store=store)

    state = _make_state("hook-session")
    runtime = MagicMock()

    with patch.object(mod, "_persist_session") as mock_persist, \
         patch("observability.tracing.current_trace_id", return_value="trace-hook"):
        result = mw.on_session_end(state, runtime)

    mock_persist.assert_called_once_with(state, "trace-hook")
    assert result is None


# ---------------------------------------------------------------------------
# 10. after_agent persistence on terminal turn
# ---------------------------------------------------------------------------

def test_after_agent_persists_on_terminal_turn(tmp_path):
    """after_agent must call _persist_session when last message is a terminal AIMessage."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    store = MagicMock()
    mw = mod.MemoryMiddleware(knowledge_store=store)

    messages = [
        HumanMessage(content="Hello"),
        AIMessage(content="Final answer with no pending tool calls."),
    ]
    state = _make_state("after-agent-terminal", messages=messages)
    runtime = MagicMock()

    with patch.object(mod, "_persist_session") as mock_persist, \
         patch("observability.tracing.current_trace_id", return_value="trace-after"):
        mw.after_agent(state, runtime)

    mock_persist.assert_called_once_with(state, "trace-after")


def test_after_agent_does_not_dump_findings_to_store(tmp_path):
    """ADR 0021: the per-turn raw knowledge dump is gone. after_agent must NOT
    write to the knowledge store — conversation knowledge is captured by the
    summarized harvest on retirement, not a raw per-turn dump."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    store = MagicMock()
    mw = mod.MemoryMiddleware(knowledge_store=store)

    messages = [
        HumanMessage(content="What's the release cadence?"),
        AIMessage(content="x" * 300),  # substantial — the old code would have dumped this
    ]
    state = _make_state("after-agent-no-dump", messages=messages)

    with patch.object(mod, "_persist_session"), \
         patch("observability.tracing.current_trace_id", return_value="t"):
        mw.after_agent(state, MagicMock())

    store.add_finding.assert_not_called()
    store.add_chunk.assert_not_called()


def test_after_agent_does_not_persist_when_tool_calls_pending(tmp_path):
    """after_agent must NOT persist when the last AIMessage has pending tool_calls."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    store = MagicMock()
    mw = mod.MemoryMiddleware(knowledge_store=store)

    ai_msg = AIMessage(content="")
    ai_msg.tool_calls = [{"id": "tc1", "name": "search", "args": {"query": "x"}}]
    messages = [
        HumanMessage(content="Search for x"),
        ai_msg,
    ]
    state = _make_state("after-agent-pending", messages=messages)
    runtime = MagicMock()

    with patch.object(mod, "_persist_session") as mock_persist, \
         patch("observability.tracing.current_trace_id", return_value="trace-pending"):
        mw.after_agent(state, runtime)

    mock_persist.assert_not_called()


def test_after_agent_does_not_persist_when_last_msg_not_ai(tmp_path):
    """after_agent must NOT persist when the last message is not an AIMessage."""
    mod = _reload_memory({"MEMORY_PATH": str(tmp_path), "PROTOAGENT_DISABLE_MEMORY": ""})

    store = MagicMock()
    mw = mod.MemoryMiddleware(knowledge_store=store)

    messages = [
        HumanMessage(content="Hello"),
        AIMessage(content="Some response."),
        HumanMessage(content="Follow-up question"),
    ]
    state = _make_state("after-agent-human-last", messages=messages)
    runtime = MagicMock()

    with patch.object(mod, "_persist_session") as mock_persist, \
         patch("observability.tracing.current_trace_id", return_value="trace-human"):
        mw.after_agent(state, runtime)

    mock_persist.assert_not_called()


# ---------------------------------------------------------------------------
# Default path resolution (no MEMORY_PATH override)
# ---------------------------------------------------------------------------

def test_default_memory_path_uses_data_home(monkeypatch):
    """Without MEMORY_PATH, the default resolves under data_home() — the
    /sandbox-in-a-container, else ~/.protoagent writable fallback every other
    store uses. The old literal /sandbox/memory/ silently skipped persistence
    on non-container hosts and, on Windows, wrote to \\sandbox at the drive
    root (caught by the desktop sidecar smoke)."""
    from pathlib import Path

    from infra.paths import data_home

    monkeypatch.delenv("MEMORY_PATH", raising=False)
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    monkeypatch.delenv("PROTOAGENT_AUTO_SCOPE", raising=False)
    sys.modules.pop("graph.middleware.memory", None)
    import graph.middleware.memory as mod

    assert Path(mod.MEMORY_PATH) == data_home() / "memory"
