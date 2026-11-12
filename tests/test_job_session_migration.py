"""ADR 0129 execution identity and crash boundaries against real SQLite stores."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from infra import job_sessions
from scheduler.local import LocalScheduler, _acquire_jobs_lock, _release_jobs_lock

PAST = "2020-01-01T00:00:00+00:00"


def scheduler(path, **kwargs):
    return LocalScheduler("migration", invoke_url="http://test", db_dir=path, **kwargs)


def legacy_store(path):
    """Build pre-S1 rows, including bookkeeping that migration must not reset."""
    s = scheduler(path)
    for job_id, context in [
        ("cron", None), ("watch-old", "system:activity"), ("chat", "chat-original"),
        ("wait:system:activity", "system:activity"), ("wait:activity", None),
    ]:
        s.add_job("original instruction", "* * * * *", job_id=job_id,
                  context_id=context, origin_session=None if job_id.startswith("wait:") else "chat-origin",
                  ttl="7d", max_fires=20)
    with s._connect() as db:
        db.execute("DROP TABLE job_sessions")
        db.execute("UPDATE jobs SET context_id = NULL WHERE id = 'cron'")
        db.execute("UPDATE jobs SET context_id = 'system:activity' WHERE id = 'watch-old'")
        db.execute("UPDATE jobs SET next_fire = ?, last_fire = ?, fire_count = 3, "
                   "consecutive_failures = 2, last_error = 'failure', last_ok = ?, "
                   "enabled = 0, fence = ?", (PAST, PAST, PAST, '["read_file"]'))
        db.execute("ALTER TABLE jobs DROP COLUMN execution_hold")
    return s


def rows(s, table):
    with s._connect() as db:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]


@pytest.mark.platform_sensitive
def test_legacy_migration_preserves_state_and_reopens_idempotently(tmp_path):
    s = legacy_store(tmp_path)
    before = rows(s, "jobs")
    s = scheduler(tmp_path)
    after = rows(s, "jobs")
    for old, new in zip(before, after, strict=True):
        assert {k: v for k, v in new.items() if k not in ("context_id", "execution_hold")} == {
            k: v for k, v in old.items() if k != "context_id"
        }
    assert s.get_job("cron").context_id.startswith("job:")
    assert s.get_job("watch-old").context_id.startswith("job:")
    assert s.get_job("cron").context_id != s.get_job("watch-old").context_id
    assert s.get_job("chat").context_id == "chat-original"
    owners = rows(s, "job_sessions")
    reopened = scheduler(tmp_path)
    assert rows(reopened, "jobs") == after
    assert rows(reopened, "job_sessions") == owners


@pytest.mark.platform_sensitive
def test_migration_crash_rolls_back_owner_assignment(tmp_path, monkeypatch):
    s = legacy_store(tmp_path)
    before = rows(s, "jobs")
    original = job_sessions.resolve
    calls = 0

    def crash(db, job):
        nonlocal calls
        result = original(db, job)
        calls += 1
        if calls == 2:
            raise RuntimeError("process stopped during assignment")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(job_sessions, "resolve", crash)
        with pytest.raises(RuntimeError, match="process stopped"):
            scheduler(tmp_path)
    # Schema additions may commit, but routing and owners are one transaction.
    assert [{k: v for k, v in r.items() if k != "execution_hold"} for r in rows(s, "jobs")] == before
    recovered = scheduler(tmp_path)
    assert recovered.get_job("cron").context_id.startswith("job:")
    assert len(rows(recovered, "job_sessions")) == len(before)


@pytest.mark.platform_sensitive
def test_migration_excludes_live_old_dispatch_writer(tmp_path):
    s = legacy_store(tmp_path)
    lock = _acquire_jobs_lock(s.path)
    try:
        with pytest.raises(RuntimeError, match="stop the scheduler"):
            scheduler(tmp_path)
        assert s.get_job("cron").context_id is None
    finally:
        _release_jobs_lock(s.path, lock)
    assert scheduler(tmp_path).get_job("cron").context_id.startswith("job:")


@pytest.mark.platform_sensitive
def test_unowned_legacy_waits_survive_recovery_pause_resume_and_direct_fire(tmp_path):
    legacy_store(tmp_path)
    s = scheduler(tmp_path)
    for job_id in ("wait:activity", "wait:system:activity"):
        job = s.get_job(job_id)
        assert job.execution_hold == job_sessions.LEGACY_WAIT_HOLD
        original_slot = job.next_fire
        s.set_enabled(job_id, True)
        # A schedule edit or enabling it cannot bypass an ownership hold.
        s.update_job(job_id, job.prompt, PAST)
        s._recover_missed_fires()
        held = s.get_job(job_id)
        assert held.next_fire == original_slot
        assert s._claim_occurrence(held) is None
        assert held not in s._claim_due_jobs(datetime.now(UTC))


@pytest.mark.platform_sensitive
def test_owner_identity_undo_replacement_and_instance_isolation(tmp_path):
    s = scheduler(tmp_path / "one")
    job = s.add_job("background", "* * * * *", job_id="stable", origin_session="chat-origin")
    assert job.context_id != scheduler(tmp_path / "two").add_job(
        "background", "* * * * *", job_id="stable").context_id
    s.update_job(job.id, "edited", "0 * * * *")
    s.set_enabled(job.id, False)
    s.set_enabled(job.id, True)
    assert s.get_job(job.id).context_id == job.context_id
    s.cancel_job(job.id)
    restored = s.add_job(job.prompt, job.schedule, job_id=job.id, restore_state=job)
    assert restored.context_id == job.context_id
    assert restored.created_at == job.created_at
    s.cancel_job(job.id)
    replacement = s.add_job("new independent generation", job.schedule, job_id=job.id)
    assert replacement.context_id != job.context_id


@pytest.mark.platform_sensitive
def test_claim_crash_and_continuation_hold_are_owner_scoped(tmp_path):
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    other = s.add_job("other", "* * * * *")
    message = s._claim_occurrence(root)
    assert message
    s = scheduler(tmp_path)
    assert s._claim_occurrence(root) is None  # no handle: unknown acceptance
    continuation = s.add_job("continue", PAST, job_id=f"wait:{root.context_id}",
                             context_id=root.context_id)
    assert s._claim_occurrence(continuation) is None
    assert s._claim_occurrence(other)  # no global Activity lock
    s._remember_task(root, "accepted")
    assert s._settle_task(root, "accepted", {
        "result": {"id": "accepted", "status": {"state": "completed"}}
    })
    root = s.get_job(root.id)
    assert s._claim_occurrence(root) is None  # pending continuation retains owner
    assert s._claim_occurrence(continuation)
    assert rows(s, "occurrences")[0]["message_id"]  # durable ledger survives


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
@pytest.mark.parametrize("orphan", [False, True])
async def test_accepted_activity_task_reopens_original_handle_without_resend(tmp_path, monkeypatch, orphan):
    """Restart both SDK handler and scheduler, backed by durable task/claim stores."""
    import asyncio
    import httpx
    from a2a.server.tasks import DatabaseTaskStore
    from a2a_impl.stores import make_sqlite_engine
    from tests._a2a_app import build_app, drain_handlers, poll_terminal

    engine = make_sqlite_engine(str(tmp_path / "tasks.db"))
    store = DatabaseTaskStore(engine)
    await store.initialize()
    runs = []
    gate = asyncio.Event()

    async def model(text, ctx, **kwargs):
        runs.append(ctx)
        await gate.wait()
        yield ("done", "finished")

    app = build_app(model, task_store=store)
    real_client = httpx.AsyncClient
    s = scheduler(tmp_path)
    job = s.add_job("legacy accepted", "* * * * *", job_id="old")
    # An old scheduler already sent into Activity and committed this exact snapshot.
    job.context_id = None
    message_id = "original-message"
    slot = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    job.next_fire = slot
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        reply = await client.post("/a2a", headers={"A2A-Version": "1.0"}, json={
            "jsonrpc": "2.0", "id": "send", "method": "SendMessage",
            "params": {"configuration": {"returnImmediately": True}, "message": {
                "role": "ROLE_USER", "parts": [{"text": job.prompt}],
                "messageId": message_id, "contextId": "system:activity",
            }},
        })
        task_id = reply.json()["result"]["task"]["id"]
        with s._connect() as db:
            db.execute("DROP TABLE job_sessions")
            db.execute("UPDATE jobs SET context_id = NULL, next_fire = ?", (slot,))
            db.execute(
                "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, "
                "expected_next, job_snapshot, task_id) VALUES (?, ?, ?, 'unknown', ?, ?, ?, ?)",
                (job.id, slot, message_id, job.created_at, slot, json.dumps(job.as_dict()), task_id),
            )
            if orphan:
                db.execute("DELETE FROM jobs")
        original_claims = rows(s, "occurrences")
        gate.set()
        await poll_terminal(client, task_id)
    await drain_handlers()
    await engine.dispose()
    engine = make_sqlite_engine(str(tmp_path / "tasks.db"))
    store = DatabaseTaskStore(engine)
    await store.initialize()
    app = build_app(model, task_store=store)
    methods = []

    class Client(real_client):
        async def post(self, *args, **kwargs):
            methods.append(kwargs["json"]["method"])
            return await super().post(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: Client(
        transport=httpx.ASGITransport(app=app), base_url="http://test"))
    events = []
    try:
        s = scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
        assert rows(s, "occurrences") == original_claims
        owner = rows(s, "job_sessions")[0]["context_id"]
        assert owner.startswith("job:")
        if not orphan:
            assert s._claim_occurrence(s.get_job(job.id)) is None
        for _ in range(3):
            await s._reconcile_occurrences()
        assert methods == ["GetTask"]
        assert len(runs) == 1
        settled = rows(s, "occurrences")[0]
        for key in ("message_id", "task_id", "job_snapshot", "next_fire", "created_at", "expected_next"):
            assert settled[key] == original_claims[0][key]
        assert settled["state"] == "settled"
        finished = [d for t, d in events if t == "turn.finished"]
        assert len(finished) == 1
        assert finished[0]["session_id"] == "system:activity"
        if not orphan:
            assert s.get_job(job.id).fire_count == 1
            assert s.get_job(job.id).context_id == owner
    finally:
        gate.set()
        await drain_handlers()
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
async def test_dispatch_and_lifecycle_use_one_persisted_identity(tmp_path, monkeypatch):
    import httpx
    from runtime.session_ids import is_valid_session_id

    real_client = httpx.AsyncClient
    sent = []
    events = []

    def receive(request):
        message = json.loads(request.content)["params"]["message"]
        sent.append(message)
        return httpx.Response(200, json={"result": {"task": {
            "id": "accepted-" + message["messageId"], "status": {"state": "completed"},
        }}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(receive)))
    s = scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
    jobs = [s.add_job("background", "* * * * *", context_id=context, origin_session="chat-source")
            for context in (None, "system:activity", "chat-explicit")]
    s = scheduler(tmp_path, event_publish=lambda t, d: events.append((t, d)))
    for job in jobs:
        assert is_valid_session_id(job.context_id)
        assert await s._fire(job)
        lifecycle = [d for t, d in events if t in ("turn.started", "turn.finished") and d["trigger"] == job.id]
        assert [d["session_id"] for d in lifecycle] == [job.context_id, job.context_id]
        assert sent[-1]["contextId"] == job.context_id
        assert s.get_job(job.id).origin_session == "chat-source"
    assert len({m["contextId"] for m in sent}) == 3
    assert jobs[-1].context_id == "chat-explicit"
    assert all(m["contextId"] != "system:activity" for m in sent)


@pytest.mark.platform_sensitive
def test_background_continuation_persists_parent_and_due_slot(tmp_path):
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    message = s._claim_occurrence(root)
    wait = s.add_job("continue later", PAST, job_id=f"wait:{root.context_id}", context_id=root.context_id)
    owner = next(r for r in rows(s, "job_sessions") if r["job_id"] == wait.id)
    assert (owner["root_job_id"], owner["root_created_at"]) == (root.id, root.created_at)
    assert owner["parent_message_id"] == message
    s._remember_task(root, "done")
    s._settle_task(root, "done", {"result": {"id": "done", "status": {"state": "completed"}}})
    s = scheduler(tmp_path)
    s._recover_missed_fires()
    assert s.get_job(wait.id).next_fire == PAST
    assert s._claim_occurrence(s.get_job(root.id)) is None
    assert s._claim_occurrence(s.get_job(wait.id))


@pytest.mark.platform_sensitive
def test_only_proven_refusal_can_retry_migrated_slot(tmp_path):
    s = legacy_store(tmp_path)
    job = s.get_job("cron")
    with s._connect() as db:
        db.execute(
            "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, "
            "expected_next, job_snapshot) VALUES (?, ?, 'same-message', 'retryable', ?, ?, ?)",
            (job.id, job.next_fire, job.created_at, job.next_fire, json.dumps(job.as_dict())),
        )
    s = scheduler(tmp_path)
    job = s.get_job(job.id)
    assert s._claim_occurrence(job) == "same-message"
    claimed = rows(s, "occurrences")[0]
    assert json.loads(claimed["job_snapshot"])["context_id"] == job.context_id
    assert job.context_id.startswith("job:")
    # A crash after claiming the retry is ambiguous again, never another send.
    assert scheduler(tmp_path)._claim_occurrence(job) is None


@pytest.mark.platform_sensitive
def test_proven_unaccepted_wait_keeps_legacy_alias_and_occurrence(tmp_path):
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    wait = s.add_job("resume", PAST, job_id="wait:system:activity", context_id=root.context_id)
    with s._connect() as db:
        # Durable dispatch evidence, not origin/prompt/timestamp guessing.
        db.execute("DELETE FROM job_sessions WHERE job_id = ?", (wait.id,))
        db.execute("UPDATE jobs SET context_id = 'system:activity' WHERE id = ?", (wait.id,))
        db.execute(
            "INSERT INTO occurrences (job_id, next_fire, message_id, state, created_at, "
            "expected_next, job_snapshot) VALUES (?, ?, 'wait-message', 'retryable', ?, ?, ?)",
            (wait.id, wait.next_fire, wait.created_at, wait.next_fire, json.dumps(wait.as_dict())),
        )
    original = rows(s, "occurrences")
    s = scheduler(tmp_path)
    migrated = s.get_job(wait.id)
    assert migrated.execution_hold is None
    assert migrated.context_id == root.context_id
    assert migrated.next_fire == wait.next_fire
    assert rows(s, "occurrences") == original
    assert s._claim_occurrence(migrated) == "wait-message"


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
@pytest.mark.parametrize("ending", ["reaction", "clear", "expired", "enqueue_failure"])
async def test_durable_watch_holds_root_until_continuation_finishes(tmp_path, monkeypatch, ending):
    from types import SimpleNamespace

    from graph.watches.controller import WatchController
    from graph.watches.store import WatchStore
    from runtime.state import STATE

    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    controller = WatchController(SimpleNamespace(), WatchStore(watch_path))
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    assert s._claim_occurrence(root)
    ok, _, watch = controller.create(
        condition="deployment finished", verifier={"type": "plugin", "check": "test:ready"},
        run_session=root.context_id, run_prompt="verify deployment",
    )
    assert ok
    s._remember_task(root, "root-task")
    s._settle_task(root, "root-task", {"result": {
        "id": "root-task", "status": {"state": "completed"},
    }})
    with s._connect() as db:
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (PAST, root.id))
    s = scheduler(tmp_path)
    controller = WatchController(SimpleNamespace(), WatchStore(watch_path))
    s._recover_missed_fires()
    root = s.get_job(root.id)
    assert root.next_fire == PAST
    assert s._claim_occurrence(root) is None
    other = s.add_job("independent", "* * * * *")
    assert s._claim_occurrence(other)
    if ending == "clear":
        controller.clear(watch.id)
    elif ending == "expired":
        await controller._finish(watch, "expired", "deadline")
    else:
        monkeypatch.setattr(STATE, "scheduler", s if ending == "reaction" else None)
        # At terminal persistence, before enqueue, even a reopened scheduler holds.
        react = controller._react

        async def check_gap(watch, reason):
            assert WatchStore(watch_path).get(watch.id).reaction_pending
            assert scheduler(tmp_path)._claim_occurrence(root) is None
            await react(watch, reason)

        monkeypatch.setattr(controller, "_react", check_gap)
        await controller._finish(watch, "met", "ready")
        s = scheduler(tmp_path)
        assert s._claim_occurrence(root) is None
        if ending == "enqueue_failure":
            assert controller.store.get(watch.id).reaction_pending
            assert controller.prune_terminal(now=10**12) == 0
            return
        reaction = s.get_job(f"watch-{watch.id}")
        assert not controller.store.get(watch.id).reaction_pending
        assert s._claim_occurrence(reaction)
        s._remember_task(reaction, "reaction-task")
        s._settle_task(reaction, "reaction-task", {"result": {
            "id": "reaction-task", "status": {"state": "completed"},
        }})
    assert s._claim_occurrence(root)


@pytest.mark.platform_sensitive
def test_unreadable_watch_does_not_release_owner(tmp_path, monkeypatch):
    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    (watch_path / "broken.json").write_text("{")
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    assert s._claim_occurrence(root) is None
    (watch_path / "broken.json").unlink()
    assert s._claim_occurrence(root)


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("repeat", [False, True])
def test_watch_ownership_excludes_hooks_and_other_sessions(tmp_path, monkeypatch, repeat):
    from graph.watches.store import WatchStore
    from graph.watches.types import Watch

    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    store = WatchStore(watch_path)
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *", job_id="watch-recurring-root")
    store.set(Watch(id="hooks", condition="ready", run_session=root.context_id))
    store.set(Watch(id="other", condition="ready", run_session="job:other", run_prompt="react"))
    assert not store.holds_execution(root.context_id)
    watch = Watch(id="owned", condition="ready", run_session=root.context_id,
                  run_prompt="react", repeat=repeat)
    store.set(watch)
    assert s._claim_occurrence(root) is None
    watch.status = "expired"
    store.set(watch)
    assert s._claim_occurrence(root)
