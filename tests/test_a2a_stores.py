"""Durable A2A stores + push-callback SSRF guard (a2a_stores.py).

Two capabilities the a2a-sdk migration dropped, restored on top of the SDK's
SQLite-backed ``DatabaseTaskStore`` / ``DatabasePushNotificationConfigStore``:

1. **Durability** — push-config + task state survive a process restart. Each
   test writes through one store instance, disposes its engine (simulating a
   restart), and reads through a *fresh* instance pointed at the same db file.
2. **SSRF guard** — the push-config callback URL is validated at set-time and
   at send-time. Loopback / RFC1918 / link-local targets are rejected; public
   targets and allowlisted hosts pass.
"""

from __future__ import annotations

import httpx
import pytest
from a2a.server.context import ServerCallContext
from a2a.types import TaskPushNotificationConfig

import a2a_stores
from a2a_stores import (
    ValidatingPushNotificationConfigStore,
    is_safe_webhook_url,
    make_sqlite_engine,
    sweep_expired_tasks,
)
from a2a.server.tasks import DatabaseTaskStore


def _ctx() -> ServerCallContext:
    return ServerCallContext()


async def _fresh_push_store(db_path: str) -> tuple[ValidatingPushNotificationConfigStore, object]:
    engine = make_sqlite_engine(db_path)
    store = ValidatingPushNotificationConfigStore(engine)
    await store.initialize()
    return store, engine


# ── (a) durability: state survives a simulated restart ──────────────────────────


@pytest.mark.asyncio
async def test_push_config_survives_restart(tmp_path):
    """Write via one store instance; read via a fresh instance on the same db file."""
    db = str(tmp_path / "a2a-push.db")
    ctx = _ctx()

    store_a, engine_a = await _fresh_push_store(db)
    await store_a.set_info(
        "task-x",
        TaskPushNotificationConfig(
            task_id="task-x", id="cfg-1", url="https://8.8.8.8/hook", token="tok"
        ),
        ctx,
    )
    await engine_a.dispose()  # simulate process exit

    store_b, engine_b = await _fresh_push_store(db)  # fresh instance, same file
    rows = await store_b.get_info("task-x", ctx)
    assert len(rows) == 1
    assert rows[0].url == "https://8.8.8.8/hook"
    assert rows[0].token == "tok"
    assert rows[0].id == "cfg-1"
    await engine_b.dispose()


@pytest.mark.asyncio
async def test_task_record_survives_restart(tmp_path):
    """A task persisted by one DatabaseTaskStore is visible to a fresh one."""
    from a2a.types import a2a_pb2

    db = str(tmp_path / "a2a-tasks.db")

    ctx = _ctx()
    engine_a = make_sqlite_engine(db)
    store_a = DatabaseTaskStore(engine_a)
    await store_a.initialize()
    task = a2a_pb2.Task(
        id="t-1",
        context_id="ctx-1",
        status=a2a_pb2.TaskStatus(state=a2a_pb2.TASK_STATE_COMPLETED),
    )
    await store_a.save(task, ctx)
    await engine_a.dispose()

    engine_b = make_sqlite_engine(db)
    store_b = DatabaseTaskStore(engine_b)
    await store_b.initialize()
    got = await store_b.get("t-1", ctx)
    assert got is not None
    assert got.id == "t-1"
    assert got.status.state == a2a_pb2.TASK_STATE_COMPLETED
    await engine_b.dispose()


@pytest.mark.asyncio
async def test_task_ttl_sweep_evicts_old_rows(tmp_path):
    """sweep_expired_tasks drops rows older than the TTL, keeps fresh ones."""
    from datetime import UTC, datetime, timedelta

    from a2a.types import a2a_pb2

    ctx = _ctx()
    db = str(tmp_path / "a2a-tasks.db")
    engine = make_sqlite_engine(db)
    store = DatabaseTaskStore(engine)
    await store.initialize()
    await store.save(a2a_pb2.Task(id="fresh", context_id="c"), ctx)
    await store.save(a2a_pb2.Task(id="stale", context_id="c"), ctx)

    # Backdate "stale" well past the 24h TTL directly in the table.
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from a2a.server.models import TaskModel

    old = datetime.now(UTC) - timedelta(hours=48)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        await session.execute(
            update(TaskModel).where(TaskModel.id == "stale").values(last_updated=old)
        )
        await session.commit()

    deleted = await sweep_expired_tasks(engine)
    assert deleted == 1
    assert await store.get("stale", ctx) is None
    assert await store.get("fresh", ctx) is not None
    await engine.dispose()


# ── (b) SSRF guard: reject private/loopback, accept public ──────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",          # loopback
        "http://localhost/hook",          # loopback by name
        "http://10.0.0.1/hook",           # RFC1918
        "http://192.168.1.5/hook",        # RFC1918
        "http://172.16.0.9/hook",         # RFC1918
        "http://169.254.169.254/latest",  # link-local (cloud metadata)
        "http://[::1]/hook",              # IPv6 loopback
        "ftp://example.com/hook",         # non-http scheme
        "not-a-url",                      # unparseable
    ],
)
def test_ssrf_guard_rejects_unsafe(url):
    assert is_safe_webhook_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://8.8.8.8/hook",           # public literal IP (no DNS needed)
        "http://93.184.216.34/hook",      # public literal IP
    ],
)
def test_ssrf_guard_accepts_public(url):
    """Public-IP literals so the accept path stays network-independent (CI may
    have no egress); a Tailscale/cloud public-IP callback is the real use case."""
    assert is_safe_webhook_url(url) is True


def test_ssrf_guard_honors_host_allowlist(monkeypatch):
    """A hostname in PUSH_NOTIFICATION_ALLOWED_HOSTS bypasses the IP check."""
    # workstacean resolves to an RFC1918 docker address by design.
    monkeypatch.setenv("PUSH_NOTIFICATION_ALLOWED_HOSTS", "workstacean")
    assert is_safe_webhook_url("http://workstacean:7860/hook") is True
    # Anything not on the list still gets the IP check (and rejected).
    assert is_safe_webhook_url("http://10.0.0.1/hook") is False


@pytest.mark.asyncio
async def test_set_info_rejects_unsafe_callback(tmp_path):
    """Set-time guard: a private callback URL raises and is not persisted."""
    db = str(tmp_path / "a2a-push.db")
    store, engine = await _fresh_push_store(db)
    ctx = _ctx()
    with pytest.raises(ValueError):
        await store.set_info(
            "task-bad",
            TaskPushNotificationConfig(task_id="task-bad", url="http://127.0.0.1/x"),
            ctx,
        )
    assert await store.get_info("task-bad", ctx) == []
    await engine.dispose()


@pytest.mark.asyncio
async def test_set_info_accepts_public_callback(tmp_path):
    db = str(tmp_path / "a2a-push.db")
    store, engine = await _fresh_push_store(db)
    ctx = _ctx()
    await store.set_info(
        "task-ok",
        TaskPushNotificationConfig(task_id="task-ok", url="https://8.8.8.8/hook"),
        ctx,
    )
    rows = await store.get_info("task-ok", ctx)
    assert len(rows) == 1 and rows[0].url == "https://8.8.8.8/hook"
    await engine.dispose()


@pytest.mark.asyncio
async def test_send_time_guard_blocks_private_without_network(tmp_path):
    """The send-time backstop returns False (no POST) for an unsafe URL."""
    db = str(tmp_path / "a2a-push.db")
    store, engine = await _fresh_push_store(db)
    async with httpx.AsyncClient() as client:
        sender = a2a_stores.build_push_sender(store, client)
        ok = await sender._dispatch_notification(
            None,
            TaskPushNotificationConfig(task_id="t", url="http://127.0.0.1/x"),
            "t",
        )
        assert ok is False
    await engine.dispose()
