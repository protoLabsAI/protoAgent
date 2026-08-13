"""Regression coverage for #2679 (QA finding F2): model.max_iterations must reach
LangGraph's recursion_limit on the STREAMING and NON-STREAMING turn paths, not just
the goal-continuation path (already covered in test_thread_id_resolver.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_chat_langgraph_stream_recursion_limit_from_config(monkeypatch):
    """The streaming path (_chat_langgraph_stream -> _run_turn_stream) must source
    recursion_limit from STATE.graph_config.max_iterations, not a hardcoded literal."""
    import runtime.state as rs
    from server import _chat_langgraph_stream

    monkeypatch.setattr(rs.STATE, "graph_config", SimpleNamespace(max_iterations=777), raising=False)

    captured: dict = {}

    async def _capture_events(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        raise TypeError("stop after capture")
        yield  # pragma: no cover — makes this an async generator

    fake_graph = MagicMock()
    fake_graph.astream_events = _capture_events
    with patch("server.STATE.graph", fake_graph):
        async for _kind, _payload in _chat_langgraph_stream("hi", "s-recur-stream"):
            pass

    assert captured["config"] is not None, "graph.astream_events was never called"
    assert captured["config"]["recursion_limit"] == 777


@pytest.mark.asyncio
async def test_chat_langgraph_non_stream_recursion_limit_from_config(monkeypatch):
    """The non-streaming path (_chat_langgraph -> _chat_langgraph_impl) sets NO
    recursion_limit at all before #2679 — this pins that the fix actually reaches it,
    sourced from STATE.graph_config.max_iterations."""
    import runtime.state as rs
    from server import _chat_langgraph

    monkeypatch.setattr(rs.STATE, "graph_config", SimpleNamespace(max_iterations=888), raising=False)

    captured: dict = {}

    async def _capture_invoke(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        raise TypeError("stop after capture")

    fake_graph = MagicMock()
    fake_graph.ainvoke = _capture_invoke
    with patch("server.STATE.graph", fake_graph):
        await _chat_langgraph("hi", "s-recur-nonstream")

    assert captured["config"] is not None, "graph.ainvoke was never called"
    assert captured["config"]["recursion_limit"] == 888


@pytest.mark.asyncio
async def test_chat_langgraph_non_stream_falls_back_when_graph_config_unset(monkeypatch):
    """Defensive fallback (matches the streaming/goal paths): a test harness or early-
    boot caller with no STATE.graph_config must still get a sane recursion_limit rather
    than an AttributeError on None.max_iterations."""
    import runtime.state as rs
    from server import _chat_langgraph

    monkeypatch.setattr(rs.STATE, "graph_config", None, raising=False)

    captured: dict = {}

    async def _capture_invoke(*args, **kwargs):
        captured["config"] = kwargs.get("config")
        raise TypeError("stop after capture")

    fake_graph = MagicMock()
    fake_graph.ainvoke = _capture_invoke
    with patch("server.STATE.graph", fake_graph):
        await _chat_langgraph("hi", "s-recur-fallback")

    assert captured["config"]["recursion_limit"] == 200
