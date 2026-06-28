"""Durable A2A stores + push-callback SSRF guard (stores.py).

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

from a2a_impl import stores
from a2a_impl.stores import (
    ValidatingPushNotificationConfigStore,
    initialize_a2a_stores,
    is_safe_webhook_url,
    make_sqlite_engine,
    reconcile_interrupted_tasks,
    sweep_expired_tasks,
    sweep_orphaned_push_configs,
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
        TaskPushNotificationConfig(task_id="task-x", id="cfg-1", url="https://8.8.8.8/hook", token="tok"),
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
        await session.execute(update(TaskModel).where(TaskModel.id == "stale").values(last_updated=old))
        await session.commit()

    deleted = await sweep_expired_tasks(engine)
    assert deleted == 1
    assert await store.get("stale", ctx) is None
    assert await store.get("fresh", ctx) is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_task_ttl_sweep_preserves_hitl_pauses(tmp_path):
    """A resumable input_required/auth_required pause must NOT be TTL-swept even
    when stale — its LangGraph checkpoint can still resume (a non-HITL stale task
    on the same sweep is still evicted)."""
    from datetime import UTC, datetime, timedelta

    from a2a.server.models import TaskModel
    from a2a.types import a2a_pb2
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import async_sessionmaker

    ctx = _ctx()
    engine = make_sqlite_engine(str(tmp_path / "a2a-tasks.db"))
    store = DatabaseTaskStore(engine)
    await store.initialize()
    await store.save(a2a_pb2.Task(id="paused", context_id="c"), ctx)
    await store.save(a2a_pb2.Task(id="dead", context_id="c"), ctx)

    old = datetime.now(UTC) - timedelta(hours=48)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as session:
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == "paused")
            .values(last_updated=old, status={"state": "TASK_STATE_INPUT_REQUIRED"})
        )
        await session.execute(
            update(TaskModel)
            .where(TaskModel.id == "dead")
            .values(last_updated=old, status={"state": "TASK_STATE_WORKING"})
        )
        await session.commit()

    deleted = await sweep_expired_tasks(engine)
    assert deleted == 1  # only the non-HITL stale row
    async with sm() as session:
        remaining = {r[0] for r in (await session.execute(select(TaskModel.id))).all()}
    assert "paused" in remaining  # resumable HITL pause survives the TTL
    assert "dead" not in remaining
    await engine.dispose()


@pytest.mark.asyncio
async def test_orphaned_push_config_sweep(tmp_path):
    """sweep_orphaned_push_configs drops push configs whose task is gone (ADR 0051),
    keeps configs for live tasks."""
    from a2a.types import a2a_pb2

    ctx = _ctx()
    task_engine = make_sqlite_engine(str(tmp_path / "a2a-tasks.db"))
    task_store = DatabaseTaskStore(task_engine)
    await task_store.initialize()
    await task_store.save(a2a_pb2.Task(id="live", context_id="c"), ctx)

    push_store, push_engine = await _fresh_push_store(str(tmp_path / "a2a-push.db"))
    for tid in ("live", "gone"):
        await push_store.set_info(
            tid,
            TaskPushNotificationConfig(task_id=tid, id="cfg", url="https://8.8.8.8/hook", token="t"),
            ctx,
        )

    swept = await sweep_orphaned_push_configs(task_engine, push_engine)
    assert swept == 1
    assert await push_store.get_info("gone", ctx) == []
    assert len(await push_store.get_info("live", ctx)) == 1
    await task_engine.dispose()
    await push_engine.dispose()


# ── (a2) restart reconciliation: interrupted tasks fail, not linger (#486) ───────


async def _seed_states(tmp_path) -> tuple:
    """A store seeded with one task per interesting state."""
    from a2a.types import a2a_pb2

    ctx = _ctx()
    engine = make_sqlite_engine(str(tmp_path / "a2a-tasks.db"))
    store = DatabaseTaskStore(engine)
    await store.initialize()
    for tid, state in [
        ("submitted", a2a_pb2.TASK_STATE_SUBMITTED),
        ("working", a2a_pb2.TASK_STATE_WORKING),
        ("waiting", a2a_pb2.TASK_STATE_INPUT_REQUIRED),
        ("done", a2a_pb2.TASK_STATE_COMPLETED),
    ]:
        await store.save(a2a_pb2.Task(id=tid, context_id="c", status=a2a_pb2.TaskStatus(state=state)), ctx)
    return store, engine, ctx


@pytest.mark.asyncio
async def test_reconcile_fails_only_interrupted_states(tmp_path):
    """submitted + working → failed; input_required + completed left alone."""
    from a2a.types import a2a_pb2

    store, engine, ctx = await _seed_states(tmp_path)
    n = await reconcile_interrupted_tasks(engine)
    assert n == 2  # only submitted + working

    assert (await store.get("submitted", ctx)).status.state == a2a_pb2.TASK_STATE_FAILED
    assert (await store.get("working", ctx)).status.state == a2a_pb2.TASK_STATE_FAILED
    # input_required is a resumable HITL/auth pause — must NOT be failed.
    assert (await store.get("waiting", ctx)).status.state == a2a_pb2.TASK_STATE_INPUT_REQUIRED
    assert (await store.get("done", ctx)).status.state == a2a_pb2.TASK_STATE_COMPLETED

    # the terminal failure carries an error message a caller can react to.
    failed = await store.get("submitted", ctx)
    assert failed.status.message.parts[0].text
    await engine.dispose()


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(tmp_path):
    """A second pass touches nothing (already-failed isn't an interrupted state)."""
    store, engine, ctx = await _seed_states(tmp_path)
    assert await reconcile_interrupted_tasks(engine) == 2
    assert await reconcile_interrupted_tasks(engine) == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_initialize_reconciles_before_sweep(tmp_path):
    """initialize_a2a_stores fails interrupted tasks at boot so they surface as
    terminal `failed` (not silently deleted)."""
    from a2a.types import a2a_pb2

    store, engine, ctx = await _seed_states(tmp_path)
    push_store = ValidatingPushNotificationConfigStore(engine)
    await initialize_a2a_stores(store, push_store)

    assert (await store.get("submitted", ctx)).status.state == a2a_pb2.TASK_STATE_FAILED
    assert (await store.get("working", ctx)).status.state == a2a_pb2.TASK_STATE_FAILED
    await engine.dispose()


# ── (b) SSRF guard: reject private/loopback, accept public ──────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/hook",  # loopback
        "http://localhost/hook",  # loopback by name
        "http://10.0.0.1/hook",  # RFC1918
        "http://192.168.1.5/hook",  # RFC1918
        "http://172.16.0.9/hook",  # RFC1918
        "http://169.254.169.254/latest",  # link-local (cloud metadata)
        "http://[::1]/hook",  # IPv6 loopback
        "ftp://example.com/hook",  # non-http scheme
        "not-a-url",  # unparseable
    ],
)
def test_ssrf_guard_rejects_unsafe(url):
    assert is_safe_webhook_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://8.8.8.8/hook",  # public literal IP (no DNS needed)
        "http://93.184.216.34/hook",  # public literal IP
    ],
)
def test_ssrf_guard_accepts_public(url):
    """Public-IP literals so the accept path stays network-independent (CI may
    have no egress); a Tailscale/cloud public-IP callback is the real use case."""
    assert is_safe_webhook_url(url) is True


def test_ssrf_guard_honors_host_allowlist(monkeypatch):
    """A hostname in PUSH_NOTIFICATION_ALLOWED_HOSTS bypasses the IP check."""
    # automaker-server resolves to an RFC1918 docker address by design.
    monkeypatch.setenv("PUSH_NOTIFICATION_ALLOWED_HOSTS", "automaker-server")
    assert is_safe_webhook_url("http://automaker-server:7860/hook") is True
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
        sender = stores.build_push_sender(store, client)
        ok = await sender._dispatch_notification(
            None,
            TaskPushNotificationConfig(task_id="t", url="http://127.0.0.1/x"),
            "t",
        )
        assert ok is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_async_paths_resolve_dns_off_the_event_loop(tmp_path, monkeypatch):
    """Both async guard call sites run ``is_safe_webhook_url`` via
    ``asyncio.to_thread``, so a slow resolver can't stall the whole process:
    ``getaddrinfo`` must execute on a worker thread, not the loop's thread."""
    import threading

    loop_thread = threading.get_ident()
    resolver_threads: list[int] = []

    def _recording_getaddrinfo(host, port, *args, **kwargs):
        resolver_threads.append(threading.get_ident())
        # (family, type, proto, canonname, sockaddr) for a public address.
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(stores.socket, "getaddrinfo", _recording_getaddrinfo)

    db = str(tmp_path / "a2a-push.db")
    store, engine = await _fresh_push_store(db)
    # Set-time path (hostname URL so the guard actually resolves).
    await store.set_info(
        "task-dns",
        TaskPushNotificationConfig(task_id="task-dns", url="https://hooks.example.test/x"),
        _ctx(),
    )
    assert resolver_threads, "set_info never hit the resolver"
    assert all(t != loop_thread for t in resolver_threads)

    # Send-time path: the guard re-resolves before the POST; refuse via a
    # resolver that "now" returns a private address — still off the loop.
    resolver_threads.clear()
    monkeypatch.setattr(
        stores.socket,
        "getaddrinfo",
        lambda host, port, *a, **kw: (
            resolver_threads.append(threading.get_ident()),
            [(2, 1, 6, "", ("10.0.0.1", 0))],
        )[1],
    )
    async with httpx.AsyncClient() as client:
        sender = stores.build_push_sender(store, client)
        ok = await sender._dispatch_notification(
            None,
            TaskPushNotificationConfig(task_id="task-dns", url="https://hooks.example.test/x"),
            "task-dns",
        )
    assert ok is False
    assert resolver_threads and all(t != loop_thread for t in resolver_threads)
    await engine.dispose()


# ── (c) upgrade guard: legacy pre-SDK 'tasks' table is dropped + recreated ──────


async def _make_legacy_tasks_db(db_path: str, rows: int = 2) -> None:
    """Create the bespoke pre-#443 ``tasks`` schema (no ``id`` column) + some rows,
    mimicking a db file left by the old a2a_task_store before the SDK migration."""
    from sqlalchemy import text

    engine = make_sqlite_engine(db_path)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "CREATE TABLE tasks ("
                "task_id TEXT PRIMARY KEY, state TEXT NOT NULL, "
                "updated_at TEXT NOT NULL, data TEXT NOT NULL)"
            )
        )
        for i in range(rows):
            await conn.execute(
                text("INSERT INTO tasks (task_id, state, updated_at, data) VALUES (:i, 'working', '2026-01-01', '{}')"),
                {"i": f"legacy-{i}"},
            )
    await engine.dispose()


@pytest.mark.asyncio
async def test_drop_legacy_task_table_drops_bespoke_schema(tmp_path):
    """A legacy ``tasks`` table (no ``id``) is detected and dropped → True."""
    from sqlalchemy import text

    db = str(tmp_path / "a2a-tasks.db")
    await _make_legacy_tasks_db(db)

    engine = make_sqlite_engine(db)
    dropped = await stores.drop_legacy_task_table(engine)
    assert dropped is True
    async with engine.begin() as conn:
        cols = [r[1] for r in (await conn.execute(text("PRAGMA table_info('tasks')"))).fetchall()]
    assert cols == []  # table gone; create_all will rebuild it
    await engine.dispose()


@pytest.mark.asyncio
async def test_drop_legacy_task_table_noop_on_fresh_and_sdk_schema(tmp_path):
    """No-op (False) when the table is absent, and again once the SDK schema exists."""
    db = str(tmp_path / "a2a-tasks.db")

    engine = make_sqlite_engine(db)
    # Fresh db, no tasks table yet.
    assert await stores.drop_legacy_task_table(engine) is False

    # SDK schema present (has ``id``) → must be left untouched.
    store = DatabaseTaskStore(engine)
    await store.initialize()
    assert await stores.drop_legacy_task_table(engine) is False
    await engine.dispose()


@pytest.mark.asyncio
async def test_initialize_recreates_sdk_schema_over_legacy_db(tmp_path):
    """End-to-end: a legacy db survives initialize_a2a_stores and a task round-trips
    (regression for 'no such column: tasks.id')."""
    from a2a.types import a2a_pb2

    db = str(tmp_path / "a2a-tasks.db")
    await _make_legacy_tasks_db(db)

    task_engine = make_sqlite_engine(db)
    task_store = DatabaseTaskStore(task_engine)
    push_store, push_engine = await _fresh_push_store(str(tmp_path / "a2a-push.db"))

    await initialize_a2a_stores(task_store, push_store)

    ctx = _ctx()
    task = a2a_pb2.Task(
        id="t-new",
        context_id="ctx-new",
        status=a2a_pb2.TaskStatus(state=a2a_pb2.TASK_STATE_COMPLETED),
    )
    await task_store.save(task, ctx)  # would 500 with the legacy schema
    got = await task_store.get("t-new", ctx)
    assert got is not None and got.id == "t-new"

    await task_engine.dispose()
    await push_engine.dispose()
