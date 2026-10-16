"""A scheduled turn whose model stream dies mid-turn is recorded as a FAILED fire.

Live (2026-10-16, jobCoach v0.206.0): a cron fire made tool calls, then the model
stream stalled and died with a connection reset. The producer did its job — the chat
stream yielded ``error``, the executor landed the task ``TASK_STATE_FAILED`` — but the
scheduler recorded ``last_ok`` / ``consecutive_failures 0``, because its reader only
looked at ``result.status`` while an ``A2A-Version: 1.0`` SendMessage answers
``{"result": {"task": Task}}``.

These tests drive the REAL path end to end: ``LocalScheduler._fire`` → the a2a-sdk
JSON-RPC app → ``ProtoAgentExecutor`` → ``server._chat_langgraph_stream`` → a graph whose
``astream_events`` raises after it has started streaming.
"""

from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest

from scheduler.local import LocalScheduler, _a2a_turn_failure
from tests._a2a_app import build_app, drain_handlers

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _fast_flush(monkeypatch):
    monkeypatch.setattr("a2a_impl.registry.FLUSH_GRACE_S", 0.02)


@pytest.fixture(autouse=True)
async def _drain_a2a_cleanup():
    yield
    await drain_handlers()


def _route_scheduler_to(app, monkeypatch, responses: list) -> None:
    """Point the scheduler's ``httpx.AsyncClient(timeout=…)`` at the in-process app,
    keeping each JSON body it receives so the test can assert the wire shape too."""

    class _Recording(_REAL_ASYNC_CLIENT):
        async def post(self, *a, **kw):
            r = await super().post(*a, **kw)
            responses.append(r.json())
            return r

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: _Recording(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30),
    )


def _row(s: LocalScheduler, job_id: str):
    db = sqlite3.connect(str(s.path))
    db.row_factory = sqlite3.Row
    try:
        return db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    finally:
        db.close()


def _scheduler(tmp_path) -> LocalScheduler:
    return LocalScheduler(agent_name="sched-wire", invoke_url="http://127.0.0.1:7870", db_dir=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("model stream died: connection reset by peer"),
        httpx.ReadError("model stream died: connection reset by peer"),
    ],
    ids=["generic", "httpx-read-error"],
)
async def test_a_turn_that_dies_mid_stream_is_a_failed_fire(tmp_path, monkeypatch, caplog, exc):
    from server import _chat_langgraph_stream

    async def _dies_mid_stream(*_a, **_kw):
        # The turn got going (an event streamed) before the model stream raised.
        yield {"event": "on_chain_start", "name": "agent", "run_id": "r", "data": {}, "metadata": {}, "tags": []}
        raise exc

    fake_graph = MagicMock()
    fake_graph.astream_events = _dies_mid_stream

    responses: list = []
    _route_scheduler_to(build_app(_chat_langgraph_stream), monkeypatch, responses)
    s = _scheduler(tmp_path)
    job = s.add_job(prompt="daily job report", schedule="0 7 * * *")

    with patch("server.STATE.graph", fake_graph), caplog.at_level("ERROR", logger="scheduler.local"):
        assert await s._fire(job) is True  # delivered…

    # …as a FAILED task, in the 1.0 envelope, carrying the turn's own error text —
    # proof the failure came from the dying stream, not from some earlier guard.
    (payload,) = responses
    task = payload["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    failure = _a2a_turn_failure(payload)
    assert failure
    if isinstance(exc, RuntimeError):
        assert "connection reset by peer" in failure

    row = _row(s, job.id)
    assert row["consecutive_failures"] == 1
    assert row["last_error"] == failure
    assert row["last_ok"] is None
    assert "turn FAILED" in caplog.text


@pytest.mark.asyncio
async def test_a_completed_turn_is_still_a_successful_fire(tmp_path, monkeypatch):
    """The reader now unwraps ``result.task`` — a healthy turn must still read as success."""

    async def stream(text, ctx, *, resume=False, caller_trace=None, **kwargs):
        yield ("tool_start", {"id": "t1", "name": "send_dm", "args": {}})
        yield ("tool_end", {"id": "t1", "name": "send_dm", "output": "sent"})
        yield ("done", "report sent")

    responses: list = []
    _route_scheduler_to(build_app(stream), monkeypatch, responses)
    s = _scheduler(tmp_path)
    job = s.add_job(prompt="daily job report", schedule="0 7 * * *")

    assert await s._fire(job) is True
    (payload,) = responses
    assert payload["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    assert _a2a_turn_failure(payload) == ""
    row = _row(s, job.id)
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None
    assert row["last_ok"]
