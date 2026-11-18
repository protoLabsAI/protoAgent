"""Default-off configuration and ordinary traffic compatibility for P1h."""

import importlib
import json

import httpx
import pytest
from sqlalchemy import text

from a2a_impl import stores
from graph import steering
from graph.config import LangGraphConfig
from runtime import turn_activity
from runtime.state import STATE
from tests._a2a_app import build_app, drain_handlers, poll_terminal, send_msg

chat = importlib.import_module("server.chat")


@pytest.mark.parametrize(
    "data, expected",
    [(None, False), ({}, False), ({"private_steering": {}}, False),
     ({"private_steering": {"enabled": False}}, False),
     ({"private_steering": {"enabled": True}}, True)],
)
def test_private_steering_config(monkeypatch, data, expected):
    config = LangGraphConfig.from_dict(data, _plugin_config={})
    assert config.private_steering_enabled is expected
    monkeypatch.setattr(STATE, "graph_config", config)
    assert chat.private_steering_enabled() is expected
    monkeypatch.setattr(STATE, "graph_config", None)
    assert chat.private_steering_enabled() is False
    assert LangGraphConfig().private_steering_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{}, {"private_steering": {"enabled": False}}])
async def test_flag_off_preserves_ordinary_steer_and_a2a(monkeypatch, tmp_path, data):
    monkeypatch.setattr(STATE, "graph_config", LangGraphConfig.from_dict(data, _plugin_config={}))
    monkeypatch.setenv("PROTOAGENT_HOME", str(tmp_path))
    steering._reset()
    turn_activity.begin("ordinary-steer")
    try:
        response = await chat.steer_or_start_turn("ordinary-steer", "keep going", "steer-1")
        assert response == {"ok": True, "id": "steer-1", "pending": 1}
        assert steering.pending_items("ordinary-steer") == [{"id": "steer-1", "text": "keep going"}]
        assert steering.drain("ordinary-steer") == [{"id": "steer-1", "text": "keep going"}]
    finally:
        turn_activity.end("ordinary-steer")
        steering._reset()

    # Only model execution is faked; SendMessage and SQLite persistence are real.
    async def stream(message, context, **kwargs):
        yield "text", f"reply to {message}"
        yield "done", f"reply to {message}"

    engine = stores.make_sqlite_engine(str(tmp_path / "tasks.db"))
    store = stores.ReasoningCoalescingTaskStore(engine, owner_resolver=stores.resolve_task_owner)
    await store.initialize()
    try:
        app = build_app(stream, task_store=store)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await send_msg(client, "hello")
            assert response.status_code == 200
            task = await poll_terminal(client, response.json()["result"]["task"]["id"])
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        assert task["artifacts"][0]["parts"][0] == {"text": "reply to hello"}
        async with engine.connect() as conn:
            rows = (await conn.execute(text(
                "SELECT history, artifacts, private_payload, private_audience, public_visible FROM tasks"
            ))).all()
        assert len(rows) == 1
        row = rows[0]
        history = json.loads(row.history)
        assert history[0]["messageId"] == "m1"
        assert history[0]["parts"] == [{"text": "hello"}]
        assert json.loads(row.artifacts) == task["artifacts"]
        assert row.private_payload is None
        assert row.private_audience is None
        # Ordinary rows retain the legacy NULL partition columns (public by default).
        assert row.public_visible is None
    finally:
        await drain_handlers()
        await engine.dispose()
