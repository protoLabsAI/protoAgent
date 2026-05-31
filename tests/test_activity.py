"""Tests for the Activity thread wiring (ADR 0003 slice 2)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import a2a_handler
from operator_api.routes import register_operator_routes


def test_notify_terminal_invokes_hook_and_is_exception_safe():
    record = SimpleNamespace(id="t1", context_id="system:activity", accumulated_text="hi")
    seen = []
    prior = a2a_handler._ON_TERMINAL[0]
    try:
        a2a_handler._ON_TERMINAL[0] = seen.append
        a2a_handler._notify_terminal(record)
        assert seen == [record]

        # A throwing hook must not propagate into the background runner.
        def boom(_):
            raise RuntimeError("nope")

        a2a_handler._ON_TERMINAL[0] = boom
        a2a_handler._notify_terminal(record)  # no raise

        # No hook registered → no-op.
        a2a_handler._ON_TERMINAL[0] = None
        a2a_handler._notify_terminal(record)
    finally:
        a2a_handler._ON_TERMINAL[0] = prior


def test_activity_route_returns_history():
    async def activity_list():
        return {
            "context_id": "system:activity",
            "messages": [
                {"role": "user", "content": "morning standup"},
                {"role": "assistant", "content": "3 PRs merged overnight."},
            ],
        }

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
        activity_list=activity_list,
    )
    client = TestClient(app)
    resp = client.get("/api/activity")
    assert resp.status_code == 200
    body = resp.json()
    assert body["context_id"] == "system:activity"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]


def test_activity_route_absent_without_callback():
    """No activity_list wired → route isn't registered (404)."""
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=_unused,
        subagent_batch=_unused,
    )
    client = TestClient(app)
    assert client.get("/api/activity").status_code == 404


async def _unused(*_a, **_k):  # pragma: no cover - placeholder callable
    return ""
