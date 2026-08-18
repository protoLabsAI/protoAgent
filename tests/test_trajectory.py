"""Tests for the trajectory writer (ADR 0102 S1, #2806).

"Model-visible means logged" at the reference level: request/response events
per model call, surface_op events at every history rewrite, per-conversation
JSONL with normalization + rotation + retirement.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from observability.trajectory import (
    TrajectoryLog,
    log_surface_op,
    message_ref,
    sha_text,
)


def _read(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


# ── the store ─────────────────────────────────────────────────────────────────


def test_thread_and_session_keys_converge_on_one_file(tmp_path):
    tl = TrajectoryLog(tmp_path)
    tl.append("a2a:chat-123", {"t": "x"})
    tl.append("chat-123", {"t": "y"})
    path = tl.path_for("chat-123")
    events = _read(path)
    assert [e["t"] for e in events] == ["x", "y"]
    assert all("ts" in e for e in events)


def test_retire_removes_log_and_backup(tmp_path):
    tl = TrajectoryLog(tmp_path)
    tl.append("s1", {"t": "x"})
    tl.retire("a2a:s1")  # thread-keyed retire hits the session-keyed file
    assert not tl.path_for("s1").exists()


def test_sha_covers_block_content_fully():
    # Block-list content hashes its JSON — two block lists differing only in a
    # non-text block must hash differently (a lossy flatten would collide).
    a = [{"type": "text", "text": "same"}, {"type": "image_url", "image_url": {"url": "u1"}}]
    b = [{"type": "text", "text": "same"}, {"type": "image_url", "image_url": {"url": "u2"}}]
    assert sha_text(a) != sha_text(b)
    assert sha_text("plain") == sha_text("plain")


def test_message_ref_carries_tool_call_ids():
    m = AIMessage(content="", id="a1", tool_calls=[{"id": "t1", "name": "x", "args": {}, "type": "tool_call"}])
    ref = message_ref(m)
    assert ref["id"] == "a1" and ref["role"] == "ai"
    assert ref["tool_calls"] == ["t1"]


def test_surface_op_shapes(tmp_path, monkeypatch):
    tl = TrajectoryLog(tmp_path)
    monkeypatch.setattr("observability.trajectory.trajectory_log", tl)
    log_surface_op("s1", "prune", cause="pressure>=60%", rewritten_ids=["m1", "m2"])
    log_surface_op("s1", "rewind", cause="operator", removed=4, kept=6)
    ops = _read(tl.path_for("s1"))
    assert ops[0]["op"] == "prune" and ops[0]["rewritten_ids"] == ["m1", "m2"]
    assert ops[1]["op"] == "rewind" and ops[1]["removed"] == 4 and ops[1]["kept"] == 6


# ── the middleware ────────────────────────────────────────────────────────────


class _Req:
    def __init__(self, messages, state=None):
        self.model = SimpleNamespace(model_name="claude-opus-4-8")
        self.system_message = SimpleNamespace(content="STABLE PROMPT")
        self.messages = list(messages)
        self.tools = [SimpleNamespace(name="read_file"), SimpleNamespace(name="web_search")]
        self.state = state or {"session_id": "sess-tj"}


def test_middleware_emits_joinable_request_refs(tmp_path, monkeypatch):
    """The reconstruction golden: the logged refs hash the STORED message bytes,
    so joining the log against the checkpoint re-derives the envelope exactly."""
    from graph.middleware.trajectory import TrajectoryMiddleware

    tl = TrajectoryLog(tmp_path)
    monkeypatch.setattr("observability.trajectory.trajectory_log", tl)

    msgs = [HumanMessage(content="the question", id="h1"), AIMessage(content="the answer", id="a1")]
    resp = SimpleNamespace(
        result=[AIMessage(content="ok", usage_metadata={"input_tokens": 100, "output_tokens": 5,
                                                        "total_tokens": 105,
                                                        "input_token_details": {"cache_read": 90}})]
    )
    out = TrajectoryMiddleware().wrap_model_call(_Req(msgs), lambda r: resp)
    assert out is resp

    req, response = _read(tl.path_for("sess-tj"))
    assert req["t"] == "request" and req["model"] == "claude-opus-4-8"
    assert req["stable_sha"] == sha_text("STABLE PROMPT")
    assert req["tools_count"] == 2 and req["tools_sha"] == sha_text("read_file\nweb_search")
    # Byte-hash-identical join against the stored messages:
    assert [m["sha"] for m in req["msgs"]] == [sha_text("the question"), sha_text("the answer")]
    assert [m["id"] for m in req["msgs"]] == ["h1", "a1"]
    assert response["t"] == "response" and response["status"] == "ok"
    assert response["usage"] == {"input": 100, "output": 5, "cache_read": 90, "cache_creation": 0}


def test_middleware_logs_the_error_class_and_reraises(tmp_path, monkeypatch):
    from graph.middleware.trajectory import TrajectoryMiddleware

    tl = TrajectoryLog(tmp_path)
    monkeypatch.setattr("observability.trajectory.trajectory_log", tl)

    def _boom(r):
        raise ValueError("rate limited")

    with pytest.raises(ValueError):
        TrajectoryMiddleware().wrap_model_call(_Req([HumanMessage(content="q", id="h1")]), _boom)
    events = _read(tl.path_for("sess-tj"))
    assert events[-1] == {**events[-1], "t": "response", "status": "error", "error": "ValueError"}


# ── the rewrite sites emit ────────────────────────────────────────────────────


def test_rewind_and_fork_emit_surface_ops(tmp_path, monkeypatch):
    from graph.rewind_op import fork_thread, rewind_thread

    tl = TrajectoryLog(tmp_path)
    monkeypatch.setattr("observability.trajectory.trajectory_log", tl)

    class _G:
        def __init__(self, threads):
            self.threads = {k: list(v) for k, v in threads.items()}

        async def aget_state(self, config):
            tid = config["configurable"]["thread_id"]
            return SimpleNamespace(values={"messages": list(self.threads.get(tid, []))})

        async def aupdate_state(self, config, update):
            pass

    msgs = [HumanMessage(content="q", id="h1"), AIMessage(content="a", id="a1"),
            HumanMessage(content="q2", id="h2"), AIMessage(content="a2", id="a2")]
    g = _G({"a2a:src": msgs})
    asyncio.run(rewind_thread(g, object(), "a2a:src", target_content="a"))
    asyncio.run(fork_thread(g, object(), "a2a:src", "a2a:dst", target_content="a"))

    src_ops = [e for e in _read(tl.path_for("src")) if e["t"] == "surface_op"]
    assert src_ops[0]["op"] == "rewind" and src_ops[0]["removed"] == 2
    dst_ops = [e for e in _read(tl.path_for("dst")) if e["t"] == "surface_op"]
    assert dst_ops[0]["op"] == "fork" and dst_ops[0]["kept"] == 2
    assert "from a2a:src" in dst_ops[0]["cause"]
