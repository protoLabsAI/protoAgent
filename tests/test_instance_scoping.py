"""Tests for multi-instance data scoping + scheduler interlock (ADR 0004)."""

from __future__ import annotations

import asyncio

import infra.paths as paths
from scheduler.local import (
    LocalScheduler,
    _acquire_jobs_lock,
    _release_jobs_lock,
)


# ── scheduler resolver: per-instance default vs explicit override ─────────────


def test_scheduler_db_path_nests_under_instance(tmp_path, monkeypatch):
    """The default jobs.db sits under the per-instance store
    (``instance_root/scheduler/<agent>/jobs.db``), so two instances don't collide."""
    monkeypatch.delenv("SCHEDULER_DB_DIR", raising=False)
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    paths.reset_instance_paths()
    from scheduler.local import _resolve_db_path

    p = _resolve_db_path(None, "myagent")
    assert p == tmp_path / "alice" / "scheduler" / "myagent" / "jobs.db"


def test_scheduler_db_dir_override_is_verbatim(tmp_path, monkeypatch):
    """``SCHEDULER_DB_DIR`` (or the ``db_dir`` arg) is an explicit override — used
    verbatim, only the agent segment appended (no instance scoping on top)."""
    monkeypatch.setenv("SCHEDULER_DB_DIR", str(tmp_path))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    from scheduler.local import _resolve_db_path

    p = _resolve_db_path(None, "myagent")
    assert p == tmp_path / "myagent" / "jobs.db"  # no "alice" segment


# ── owner-lock interlock ─────────────────────────────────────────────────────


def test_jobs_lock_excludes_a_second_holder(tmp_path):
    db = tmp_path / "jobs.db"
    first = _acquire_jobs_lock(db)
    assert first is not None
    assert _acquire_jobs_lock(db) is None  # second holder refused
    _release_jobs_lock(db, first)
    again = _acquire_jobs_lock(db)
    assert again is not None  # available after release
    _release_jobs_lock(db, again)


def test_second_scheduler_on_same_db_does_not_poll(tmp_path):
    async def run():
        a = LocalScheduler("agentA", invoke_url="http://127.0.0.1:7870", db_dir=str(tmp_path))
        b = LocalScheduler("agentA", invoke_url="http://127.0.0.1:7871", db_dir=str(tmp_path))
        assert a.path == b.path  # same jobs.db (same agent + dir)
        await a.start()
        await b.start()  # interlock: b must NOT acquire the lock / poll A's db
        # A owns the lock and polls. B does NOT acquire it (so it never races A),
        # but it doesn't give up either — it schedules a background retry (so a
        # transient boot-time overlap self-heals rather than killing the scheduler).
        a_owns = a._lock_fd is not None
        b_waits = b._lock_fd is None and b._task is not None
        await a.stop()
        await b.stop()
        # After A releases, a fresh scheduler can claim it.
        c = LocalScheduler("agentA", invoke_url="http://127.0.0.1:7872", db_dir=str(tmp_path))
        await c.start()
        c_owns = c._lock_fd is not None
        await c.stop()
        return a_owns, b_waits, c_owns

    a_owns, b_waits, c_owns = asyncio.run(run())
    assert a_owns is True
    assert b_waits is True  # B waits for the lock (retry), never polls A's db
    assert c_owns is True
