"""Push-notification config store (A2A 1.0).

The bespoke ``A2APushStore`` was retired in the a2a-sdk migration — a2a-sdk now
owns push-config persistence via ``InMemoryPushNotificationConfigStore``, wired
into the ``DefaultRequestHandler`` in ``server.py``. These tests lock the same
behaviors the bespoke store guaranteed (set→get roundtrip, upsert by config id,
delete, missing→empty) against the SDK store, in the 1.0
``TaskPushNotificationConfig`` shape.
"""

from __future__ import annotations

import pytest
from a2a.server.context import ServerCallContext
from a2a.server.tasks import InMemoryPushNotificationConfigStore
from a2a.types import TaskPushNotificationConfig


def _store() -> InMemoryPushNotificationConfigStore:
    return InMemoryPushNotificationConfigStore()


def _ctx() -> ServerCallContext:
    return ServerCallContext()


@pytest.mark.asyncio
async def test_set_get_roundtrip():
    s, ctx = _store(), _ctx()
    await s.set_info(
        "task-1",
        TaskPushNotificationConfig(task_id="task-1", id="cfg-1", url="https://example.com/hook", token="sek"),
        ctx,
    )
    rows = await s.get_info("task-1", ctx)
    assert len(rows) == 1
    assert rows[0].url == "https://example.com/hook"
    assert rows[0].token == "sek"
    assert rows[0].id == "cfg-1"


@pytest.mark.asyncio
async def test_set_upserts_same_config_id():
    """Re-setting the same config id replaces it rather than duplicating."""
    s, ctx = _store(), _ctx()
    await s.set_info("task-1", TaskPushNotificationConfig(task_id="task-1", id="c", url="https://a/hook", token="t1"), ctx)
    await s.set_info("task-1", TaskPushNotificationConfig(task_id="task-1", id="c", url="https://b/hook", token="t2"), ctx)
    rows = await s.get_info("task-1", ctx)
    assert len(rows) == 1
    assert rows[0].url == "https://b/hook" and rows[0].token == "t2"


@pytest.mark.asyncio
async def test_delete():
    s, ctx = _store(), _ctx()
    await s.set_info("task-1", TaskPushNotificationConfig(task_id="task-1", id="c", url="https://a/hook"), ctx)
    await s.delete_info("task-1", ctx)
    assert await s.get_info("task-1", ctx) == []


@pytest.mark.asyncio
async def test_get_missing_returns_empty():
    assert await _store().get_info("nope", _ctx()) == []


@pytest.mark.asyncio
async def test_id_defaults_to_task_id_when_unset():
    """A config with no explicit id is keyed by the task id (SDK behavior)."""
    s, ctx = _store(), _ctx()
    await s.set_info("task-9", TaskPushNotificationConfig(task_id="task-9", url="https://a/hook"), ctx)
    rows = await s.get_info("task-9", ctx)
    assert rows[0].id == "task-9"
