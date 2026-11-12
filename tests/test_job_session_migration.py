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
        db.execute("UPDATE jobs SET context_id = NULL WHERE id IN ('cron', 'wait:activity')")
        db.execute("UPDATE jobs SET context_id = 'system:activity' "
                   "WHERE id IN ('watch-old', 'wait:system:activity')")
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

    def crash(db, job, **kwargs):
        nonlocal calls
        result = original(db, job, **kwargs)
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


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
async def test_live_old_dispatch_writer_defers_migration_until_lock_acquired(tmp_path):
    """A held owner-lock at construction is the normal restart/redeploy overlap,
    not an error: raising made ``agent_init`` (which wraps the constructor in
    ``except Exception``) run scheduler-less until the NEXT restart. Instead the
    migration waits for start()'s background lock-retry — the store is excluded
    from routing changes while the old dispatch writer lives, then migrated."""
    import asyncio

    s = legacy_store(tmp_path)
    lock = _acquire_jobs_lock(s.path)
    deferred = None
    try:
        deferred = scheduler(tmp_path)  # must not raise into scheduler-less fallback
        deferred._LOCK_RETRY_SECONDS = 0.01
        # The live owner's store is untouched while it still holds the lock,
        # even across start() — which waits in the background instead of failing.
        assert deferred.get_job("cron").context_id is None
        await deferred.start()
        assert deferred._task is not None
        await asyncio.sleep(0.05)
        assert deferred.get_job("cron").context_id is None
    finally:
        _release_jobs_lock(s.path, lock)
    try:
        migrated = None
        for _ in range(500):
            migrated = deferred.get_job("cron").context_id
            if migrated:
                break
            await asyncio.sleep(0.02)
        assert migrated and migrated.startswith("job:")
        # The full migration ran through the deferred path, holds included.
        assert deferred.get_job("wait:system:activity").execution_hold == job_sessions.LEGACY_WAIT_HOLD
    finally:
        await deferred.stop()
    # Reopening with the lock free re-runs the migration idempotently.
    assert scheduler(tmp_path).get_job("cron").context_id == migrated


@pytest.mark.platform_sensitive
def test_corrupt_store_logs_and_survives_construction(tmp_path):
    """Schema/migration DatabaseErrors stay non-fatal (pre-ADR-0129 behavior):
    the constructor logs and returns a scheduler whose reads fail closed, rather
    than raising into agent_init's run-scheduler-less-until-next-restart catch."""
    base = tmp_path / "migration"
    base.mkdir()
    (base / "jobs.db").write_bytes(b"this is not a sqlite database")
    s = scheduler(tmp_path)
    assert s.list_jobs() == []


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
def test_new_activity_waits_get_live_owners_and_fire(tmp_path):
    """The legacy hold covers only rows ``migrate`` discovers. The wait tool still
    writes ``wait:system:activity`` (an Activity/inbox turn) and a sessionless
    ``wait:activity`` for NEW waits and reports success — so a freshly created job
    must receive a live ``job:`` owner, stay claimable, and survive reopen unheld."""
    s = scheduler(tmp_path)
    activity = s.add_job("resume the sweep", PAST,
                         job_id="wait:system:activity", context_id="system:activity")
    sessionless = s.add_job("resume", PAST, job_id="wait:activity")
    for job in (activity, sessionless):
        assert job.execution_hold is None
        assert job.context_id.startswith("job:")
    assert activity.context_id != sessionless.context_id
    due = {j.id for j in s._claim_due_jobs(datetime.now(UTC))}
    assert {"wait:system:activity", "wait:activity"} <= due
    assert s._claim_occurrence(activity)
    # Reopening re-runs the idempotent migration without re-holding live owners.
    s = scheduler(tmp_path)
    reopened = s.get_job("wait:activity")
    assert reopened.execution_hold is None
    assert reopened.context_id == sessionless.context_id
    assert s._claim_occurrence(reopened)


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


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
async def test_unrouted_fire_is_refused_and_held_never_sent_to_activity(tmp_path, monkeypatch):
    """Defense in depth for the dispatch invariant: a row that somehow reaches
    ``_fire`` without an isolated owner (hand-edited store, failed migration) is
    refused and held — never POSTed to the shared Activity session. Enforced with
    an explicit check, not a bare ``assert``, so ``python -O`` keeps it."""
    import httpx

    posts = []

    def receive(request):
        posts.append(request)
        return httpx.Response(200, json={})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(receive)))
    s = scheduler(tmp_path)
    job = s.add_job("sweep", "* * * * *", job_id="sweep")
    with s._connect() as db:
        db.execute("UPDATE jobs SET context_id = 'system:activity' WHERE id = ?", (job.id,))
    job.context_id = "system:activity"
    assert await s._fire(job) is False
    assert posts == []
    held = s.get_job(job.id)
    assert held.execution_hold
    assert s._claim_occurrence(held) is None


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
def test_unreadable_watch_file_does_not_hold_unrelated_owners(tmp_path, monkeypatch):
    """One corrupt watch file is skipped+logged like ``all()`` does: it can never
    tick, react or be pruned (``prune_terminal`` never sees it), so treating it as
    "held" would park every job-owned claim and missed-fire recovery behind it
    forever. A READABLE armed watch on an owner still holds that owner."""
    from graph.watches.store import WatchStore
    from graph.watches.types import Watch

    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    (watch_path / "broken.json").write_text("{")
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    assert s._claim_occurrence(root)  # the corrupt file holds nothing
    other = s.add_job("other", "* * * * *")
    WatchStore(watch_path).set(Watch(id="armed", condition="ready",
                                     run_session=other.context_id, run_prompt="react"))
    assert s._claim_occurrence(other) is None  # a readable armed watch still holds


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


@pytest.mark.platform_sensitive
@pytest.mark.parametrize("pending_id", ["wait:chat", "watch-chat"])
def test_future_interactive_continuation_does_not_hold_unrelated_schedule(tmp_path, pending_id):
    s = scheduler(tmp_path)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    pending = s.add_job("later continuation", future, job_id=pending_id, context_id="chat")
    due = s.add_job("independent reminder", PAST, context_id="chat")
    s = scheduler(tmp_path)
    assert s._claim_occurrence(s.get_job(due.id))
    assert s.get_job(pending.id).next_fire == future
    # Shared interactive contexts hold per IDENTITY only (ADR 0129 D6): another
    # job's unresolved invocation in the same chat never blocks this one. One
    # pending wait per chat stays the wait tool's supersession rule (ADR 0053),
    # enforced at creation, not at claim time.
    assert s._claim_occurrence(s.get_job(pending.id))


@pytest.mark.platform_sensitive
def test_stale_interactive_one_shot_follows_stale_policy_despite_unrelated_wait(tmp_path):
    """An unrelated future wait in the same chat exempts nothing from stale-fire
    recovery (ADR 0129 D6); only the job's OWN unresolved work is preserved."""
    s = scheduler(tmp_path)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    s.add_job("later continuation", future, job_id="wait:chat", context_id="chat")
    stale = s.add_job("forgotten reminder", PAST, context_id="chat")
    quarantined = s.add_job("sent but unresolved", PAST, context_id="chat")
    assert s._claim_occurrence(quarantined)
    s = scheduler(tmp_path)
    s._recover_missed_fires()
    # The stale one-shot is dropped — it never fires days late into the chat.
    assert s.get_job(stale.id) is None
    # Its neighbor's own unresolved send stays quarantined, never dropped or resent.
    assert s.get_job(quarantined.id) is not None
    assert s._claim_occurrence(s.get_job(quarantined.id)) is None
    assert s.get_job("wait:chat").next_fire == future


@pytest.mark.platform_sensitive
def test_orphaned_unknown_claim_holds_only_its_own_generation(tmp_path):
    """A handle-less unknown occurrence (crash inside the send window) is not
    reconcilable — recovery needs a persisted task handle — so it quarantines
    only its OWN generation (ADR 0129 D6 per-identity holds): replacements of
    the same job id and other jobs aimed at the same chat still fire."""
    s = scheduler(tmp_path)
    first = s.add_job("resume", PAST, job_id="wait:chat", context_id="chat")
    assert s._claim_occurrence(first)
    s._finish_dispatch(first, "unknown")  # delivery unknown; no handle persisted
    s.cancel_job(first.id)
    s = scheduler(tmp_path)
    replacement = s.add_job("resume again", PAST, job_id="wait:chat", context_id="chat")
    assert replacement.created_at != first.created_at
    assert s._claim_occurrence(replacement)
    assert s._claim_occurrence(s.add_job("reminder", PAST, context_id="chat"))
    # The orphan itself stays quarantined — unresolved, and never resent.
    orphan = next(r for r in rows(s, "occurrences") if r["created_at"] == first.created_at)
    assert orphan["state"] == "unknown" and orphan["task_id"] is None
    assert all(c["created_at"] != first.created_at for c in s._pending_reconciliation())


@pytest.mark.platform_sensitive
def test_handleless_orphan_does_not_capture_replacement_owner(tmp_path, monkeypatch):
    """The job:-owned variant of the orphan rule. A claimed occurrence with NO task
    handle can never be reconciled (there is nothing to GetTask) and is never
    resent — it stays quarantined, holding its own generation and its own owner.
    ``resolve`` must not hand that stuck owner to a later generation of the same
    job id: cancel-and-recreate runs under a fresh owner and fires (ADR 0129 D6)."""
    monkeypatch.setenv("WATCH_PATH", str(tmp_path / "watches"))
    s = scheduler(tmp_path)
    first = s.add_job("report", PAST, job_id="report")
    assert first.context_id.startswith("job:")
    assert s._claim_occurrence(first)
    s = scheduler(tmp_path)  # crash between claim and send: no handle persisted
    s.cancel_job(first.id)
    replacement = s.add_job("report again", PAST, job_id="report")
    assert replacement.context_id.startswith("job:")
    assert replacement.context_id != first.context_id  # the stuck owner is not inherited
    assert s._claim_occurrence(replacement)  # ...so cancel-and-recreate escapes it
    # The orphan stays exactly as quarantined as before: unresolved, never resent.
    orphan = next(r for r in rows(s, "occurrences") if r["created_at"] == first.created_at)
    assert orphan["state"] == "claimed" and orphan["task_id"] is None
    assert all(c["created_at"] != first.created_at for c in s._pending_reconciliation())


@pytest.mark.platform_sensitive
def test_accepted_predecessor_captures_replacement_behind_same_owner(tmp_path, monkeypatch):
    """Contrast case: an ACCEPTED unresolved occurrence carries a persisted task
    handle, so durable GetTask reconciliation can settle it. A replacement then
    reuses the SAME owner and queues behind the accepted work (ADR 0129 D6) —
    never resending it under a new identity — and runs once it settles."""
    monkeypatch.setenv("WATCH_PATH", str(tmp_path / "watches"))
    s = scheduler(tmp_path)
    first = s.add_job("report", PAST, job_id="report")
    assert s._claim_occurrence(first)
    s._remember_task(first, "accepted-task")
    s = scheduler(tmp_path)  # crash after acceptance: the handle survived
    s.cancel_job(first.id)
    replacement = s.add_job("report again", PAST, job_id="report")
    assert replacement.context_id == first.context_id
    assert s._claim_occurrence(replacement) is None  # queued behind accepted work
    assert [c["task_id"] for c in s._pending_reconciliation()] == ["accepted-task"]
    assert s._settle_task(first, "accepted-task", {"result": {
        "id": "accepted-task", "status": {"state": "completed"},
    }})
    assert s._claim_occurrence(s.get_job("report"))


@pytest.mark.platform_sensitive
def test_stale_replacement_follows_stale_policy_despite_predecessors_orphan(tmp_path):
    """Startup recovery exempts a job from stale-fire policy only for its OWN
    unresolved occurrence. An orphaned claim left by a replaced generation must
    not keep the newer version past-due forever — it would then fire days late."""
    s = scheduler(tmp_path)
    first = s.add_job("resume", PAST, job_id="wait:chat", context_id="chat")
    assert s._claim_occurrence(first)
    s._finish_dispatch(first, "unknown")
    s.cancel_job(first.id)
    s.add_job("resume again", PAST, job_id="wait:chat", context_id="chat")
    cron = s.add_job("sweep", "* * * * *", job_id="chat-cron", context_id="chat")
    with s._connect() as db:
        db.execute("UPDATE jobs SET next_fire = ? WHERE id = ?", (PAST, cron.id))
    s = scheduler(tmp_path)
    s._recover_missed_fires()
    # The stale one-shot replacement is dropped; the stale cron rolls forward.
    assert s.get_job("wait:chat") is None
    assert s.get_job("chat-cron").next_fire != PAST
    # The orphan stays exactly as quarantined as before — present, unresolved.
    orphan = next(r for r in rows(s, "occurrences") if r["created_at"] == first.created_at)
    assert orphan["state"] == "unknown"


@pytest.mark.platform_sensitive
def test_replacement_queues_behind_predecessors_outstanding_wait(tmp_path):
    """Replacing a job whose fire settled with a wait still outstanding reuses the
    SAME owner and queues behind the continuation (ADR 0129 D6) — a fresh owner
    would let the replacement walk past the predecessor's pending resume."""
    s = scheduler(tmp_path)
    job = s.add_job("deploy report", PAST, job_id="deploy")
    message = s._claim_occurrence(job)
    assert message
    # The fire's turn parks a wait continuation in the owner, then settles.
    wait = s.add_job("resume deploy report", PAST,
                     job_id=f"wait:{job.context_id}", context_id=job.context_id)
    s._remember_task(job, "first-task")
    assert s._settle_task(job, "first-task", {"result": {
        "id": "first-task", "status": {"state": "completed"},
    }})
    assert s.get_job(job.id) is None  # the settled one-shot is retired
    replacement = s.add_job("deploy report again", PAST, job_id="deploy")
    assert replacement.context_id == job.context_id
    assert replacement.created_at != job.created_at
    assert s._claim_occurrence(replacement) is None
    # Restart: recovery keeps both the queued replacement and its owner's wait.
    s = scheduler(tmp_path)
    s._recover_missed_fires()
    assert s.get_job("deploy") is not None
    assert s._claim_occurrence(s.get_job("deploy")) is None
    # The continuation enters first; once it settles, the replacement runs.
    wait = s.get_job(wait.id)
    assert s._claim_occurrence(wait)
    s._remember_task(wait, "wait-task")
    assert s._settle_task(wait, "wait-task", {"result": {
        "id": "wait-task", "status": {"state": "completed"},
    }})
    assert s._claim_occurrence(s.get_job("deploy"))


@pytest.mark.platform_sensitive
def test_replacement_with_armed_watch_retains_owner_until_cleared(tmp_path, monkeypatch):
    from graph.watches.store import WatchStore
    from graph.watches.types import Watch

    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    store = WatchStore(watch_path)
    s = scheduler(tmp_path)
    job = s.add_job("deploy", PAST, job_id="deploy")
    assert s._claim_occurrence(job)
    # The fire's turn arms a watch on the owner, then the occurrence settles.
    store.set(Watch(id="deployed", condition="deployment done",
                    run_session=job.context_id, run_prompt="verify deployment"))
    s._remember_task(job, "task")
    assert s._settle_task(job, "task", {"result": {
        "id": "task", "status": {"state": "completed"},
    }})
    replacement = s.add_job("deploy again", PAST, job_id="deploy")
    assert replacement.context_id == job.context_id
    assert s._claim_occurrence(replacement) is None
    store.clear("deployed")
    replacement = s.get_job("deploy")
    assert s._claim_occurrence(replacement)
    s._remember_task(replacement, "task-2")
    assert s._settle_task(replacement, "task-2", {"result": {
        "id": "task-2", "status": {"state": "completed"},
    }})
    # With nothing of the predecessor left unresolved, a re-add mints a new owner.
    fresh = s.add_job("deploy fresh", PAST, job_id="deploy")
    assert fresh.context_id != job.context_id


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
async def test_repeating_watch_second_reaction_waits_for_first_continuation(tmp_path, monkeypatch):
    """A later reaction of a repeating watch is a NEW independent occurrence: it
    waits for an earlier reaction's outstanding wait, while that wait — the live
    continuation the owner actually yielded for — does enter (ADR 0129 D6)."""
    from types import SimpleNamespace

    from graph.watches.controller import WatchController
    from graph.watches.store import WatchStore
    from runtime.state import STATE

    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    s = scheduler(tmp_path)
    monkeypatch.setattr(STATE, "scheduler", s)
    controller = WatchController(SimpleNamespace(), WatchStore(watch_path))
    root = s.add_job("root", "* * * * *")
    ok, _, watch = controller.create(
        condition="ready", verifier={"type": "plugin", "check": "test:ready"},
        run_session=root.context_id, run_prompt="react", repeat=True,
    )
    assert ok
    await controller._react(watch, "first fire")
    reaction = s.get_job(f"watch-{watch.id}")
    # The watch stays armed (repeating) but never holds its OWN reaction out.
    assert s._claim_occurrence(reaction)
    # Reaction 1's turn parks a wait continuation, then settles.
    wait = s.add_job("resume reaction", PAST,
                     job_id=f"wait:{root.context_id}", context_id=root.context_id)
    s._remember_task(reaction, "reaction-1")
    assert s._settle_task(reaction, "reaction-1", {"result": {
        "id": "reaction-1", "status": {"state": "completed"},
    }})
    await controller._react(watch, "second fire")
    second = s.get_job(f"watch-{watch.id}")
    assert second.created_at != reaction.created_at
    assert second.context_id == root.context_id
    # The new independent occurrence waits behind reaction 1's pending wait...
    assert s._claim_occurrence(second) is None
    # ...while reaction 1's own continuation enters.
    wait = s.get_job(wait.id)
    assert s._claim_occurrence(wait)
    s._remember_task(wait, "wait-task")
    assert s._settle_task(wait, "wait-task", {"result": {
        "id": "wait-task", "status": {"state": "completed"},
    }})
    assert s._claim_occurrence(s.get_job(f"watch-{watch.id}"))


@pytest.mark.platform_sensitive
def test_parentless_sibling_continuations_drain_oldest_first_not_deadlock(tmp_path, monkeypatch):
    """Two continuations enqueued after the owner's turn settled (two watch
    reactions, say) both carry no parent occurrence — resolve found nothing in
    flight to yield for. Neither may walk past a live yielded-for continuation,
    but each refusing on the OTHER forever would park both and the root cron
    behind them, with startup recovery (which deliberately skips continuations)
    never releasing the owner. They drain oldest generation first instead."""
    monkeypatch.setenv("WATCH_PATH", str(tmp_path / "watches"))
    s = scheduler(tmp_path)
    root = s.add_job("root", "* * * * *")
    assert s._claim_occurrence(root)
    s._remember_task(root, "root-task")
    assert s._settle_task(root, "root-task", {"result": {
        "id": "root-task", "status": {"state": "completed"},
    }})
    first = s.add_job("first reaction", PAST, job_id="watch-one", context_id=root.context_id)
    second = s.add_job("second reaction", PAST, job_id="watch-two", context_id=root.context_id)
    owners = {r["job_id"]: r for r in rows(s, "job_sessions")}
    assert owners["watch-one"]["parent_message_id"] is None
    assert owners["watch-two"]["parent_message_id"] is None
    # Restart: recovery pins both continuations on their original due slots.
    s = scheduler(tmp_path)
    s._recover_missed_fires()
    # The root cron waits behind both; the younger sibling waits for the older.
    assert s._claim_occurrence(s.get_job(root.id)) is None
    assert s._claim_occurrence(s.get_job(second.id)) is None
    first = s.get_job(first.id)
    assert s._claim_occurrence(first)
    # While the head runs, the owner is busy for every other claim.
    assert s._claim_occurrence(s.get_job(second.id)) is None
    s._remember_task(first, "reaction-1")
    assert s._settle_task(first, "reaction-1", {"result": {
        "id": "reaction-1", "status": {"state": "completed"},
    }})
    assert s._claim_occurrence(s.get_job(root.id)) is None
    second = s.get_job(second.id)
    assert s._claim_occurrence(second)
    s._remember_task(second, "reaction-2")
    assert s._settle_task(second, "reaction-2", {"result": {
        "id": "reaction-2", "status": {"state": "completed"},
    }})
    # With the owner's queue drained, the root cron fires again.
    assert s._claim_occurrence(s.get_job(root.id))


@pytest.mark.asyncio
@pytest.mark.platform_sensitive
@pytest.mark.parametrize("boundary", ["before_enqueue", "enqueue_failure", "queued", "accepted", "completed"])
async def test_terminal_watch_recovery_deduplicates_durable_enqueue(tmp_path, monkeypatch, boundary):
    from types import SimpleNamespace

    from graph.fence_scope import fence_scope
    from graph.watches.controller import WatchController
    from graph.watches.store import WatchStore
    from runtime.state import STATE

    watch_path = tmp_path / "watches"
    watch_path.mkdir()
    monkeypatch.setenv("WATCH_PATH", str(watch_path))
    s = scheduler(tmp_path)
    monkeypatch.setattr(STATE, "scheduler", s)
    c = WatchController(SimpleNamespace(), WatchStore(watch_path))
    root = s.add_job("root", "* * * * *")
    with fence_scope(["read_file"]):
        _, _, watch = c.create(
            condition="ready", verifier={"type": "plugin", "check": "test:ready"},
            run_prompt="inspect result", run_session=root.context_id,
        )
    if boundary == "before_enqueue":
        async def crash(*args, **kwargs):
            raise RuntimeError("crash before enqueue")
        monkeypatch.setattr(c, "_react", crash)
        with pytest.raises(RuntimeError, match="crash before enqueue"):
            await c._finish(watch, "met", "ready")
    elif boundary == "enqueue_failure":
        monkeypatch.setattr(STATE, "scheduler", None)
        await c._finish(watch, "met", "ready")
    else:
        # Commit the enqueue, then lose the acknowledgement in the watch file.
        real_write = c._write_if_current

        def lose_ack(watch, gen):
            if not watch.reaction_pending:
                raise OSError("crash after enqueue commit")
            return real_write(watch, gen)

        monkeypatch.setattr(c, "_write_if_current", lose_ack)
        await c._finish(watch, "met", "ready")
    assert WatchStore(watch_path).get(watch.id).reaction_pending
    assert s._claim_occurrence(root) is None
    reaction_id = f"watch-{watch.id}"
    reaction = s.get_job(reaction_id)
    if boundary in ("accepted", "completed"):
        assert s._claim_occurrence(reaction)
        s._remember_task(reaction, "reaction-task")
        if boundary == "completed":
            assert s._settle_task(reaction, "reaction-task", {"result": {
                "id": "reaction-task", "status": {"state": "completed"},
            }})
            assert s.get_job(reaction_id) is None
    before = rows(s, "occurrences")
    s = scheduler(tmp_path)
    monkeypatch.setattr(STATE, "scheduler", s)
    c = WatchController(SimpleNamespace(), WatchStore(watch_path))
    # Recovery retries only the durable enqueue, not the verifier or reaction hooks.
    async def forbidden(*args, **kwargs):
        raise AssertionError("terminal recovery replayed verifier/hooks")
    monkeypatch.setattr("graph.watches.controller.run_verifier", forbidden)
    monkeypatch.setattr("graph.watches.hooks.fire_watch_hook", forbidden)
    await c.tick_all()
    await c.evaluate_now(watch.id)
    await c.tick_all()
    assert not c.store.get(watch.id).reaction_pending
    assert rows(s, "occurrences") == before
    assert len(rows(s, "job_enqueue_receipts")) == 1
    if boundary == "completed":
        assert s.get_job(reaction_id) is None
        assert s._claim_occurrence(root)
    else:
        restored = s.get_job(reaction_id)
        assert restored.context_id == root.context_id
        assert restored.fence == ["read_file"]
        if reaction:
            assert restored.created_at == reaction.created_at
            if boundary == "queued":
                assert restored.as_dict() == reaction.as_dict()
        assert s._claim_occurrence(root) is None


@pytest.mark.platform_sensitive
def test_enqueue_receipt_and_replacement_commit_atomically(tmp_path):
    import sqlite3

    s = scheduler(tmp_path)
    original = s.add_job("old reaction", PAST, job_id="watch-reused", enqueue_key="first")
    with s._connect() as db:
        db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON job_enqueue_receipts "
                   "BEGIN SELECT RAISE(ABORT, 'receipt unavailable'); END")
    with pytest.raises(ValueError) as error:
        s.add_job("new reaction", PAST, job_id=original.id, enqueue_key="second")
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    s = scheduler(tmp_path)
    assert s.get_job(original.id).as_dict() == original.as_dict()
    assert len(rows(s, "job_enqueue_receipts")) == 1
    with s._connect() as db:
        db.execute("DROP TRIGGER fail_receipt")
    replacement = s.add_job("new reaction", PAST, job_id=original.id, enqueue_key="second")
    assert replacement.created_at != original.created_at
    # A late retry of the first reaction cannot replace the newer watch's turn.
    assert s.add_job("old reaction", PAST, job_id=original.id, enqueue_key="first") == original
    assert s.get_job(original.id) == replacement
