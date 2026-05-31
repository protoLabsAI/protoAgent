"""Tests for multi-instance data scoping + scheduler interlock (ADR 0004)."""

from __future__ import annotations

import asyncio

import paths
from scheduler.local import (
    LocalScheduler,
    _acquire_jobs_lock,
    _release_jobs_lock,
)


# ── scope_leaf ───────────────────────────────────────────────────────────────


def test_scope_leaf_is_noop_without_instance(monkeypatch):
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    assert str(paths.scope_leaf("/sandbox/checkpoints.db")) == "/sandbox/checkpoints.db"
    assert str(paths.scope_leaf("/sandbox/knowledge/agent.db")) == "/sandbox/knowledge/agent.db"


def test_scope_leaf_nests_under_instance(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    assert str(paths.scope_leaf("/sandbox/checkpoints.db")) == "/sandbox/alice/checkpoints.db"
    assert str(paths.scope_leaf("/sandbox/knowledge/agent.db")) == "/sandbox/knowledge/alice/agent.db"
    assert str(paths.scope_leaf("/sandbox/workflows")) == "/sandbox/alice/workflows"


def test_scope_leaf_sanitizes_dangerous_ids(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "../../etc")
    out = paths.scope_leaf("/sandbox/x.db")
    # Path separators are flattened to a single segment, so the id can't escape
    # the intended directory (no "/" in the inserted segment).
    assert out == __import__("pathlib").Path("/sandbox/.._.._etc/x.db")
    assert "/" not in out.parent.name  # single sanitized segment
    assert out.parts[:2] == ("/", "sandbox")  # stays under the base


def test_two_instances_get_disjoint_paths(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    a = str(paths.scope_leaf("/sandbox/checkpoints.db"))
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "bob")
    b = str(paths.scope_leaf("/sandbox/checkpoints.db"))
    assert a != b


# ── scheduler resolver honors the instance id ────────────────────────────────


def test_scheduler_db_path_nests_under_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "alice")
    monkeypatch.setenv("SCHEDULER_DB_DIR", str(tmp_path))
    from scheduler.local import _resolve_db_path

    p = _resolve_db_path(None, "myagent")
    # .../alice/myagent/jobs.db — instance segment present, agent segment present
    assert "alice" in p.parts and "myagent" in p.parts and p.name == "jobs.db"


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


def test_second_scheduler_on_same_db_does_not_start_polling(tmp_path):
    async def run():
        a = LocalScheduler("agentA", invoke_url="http://127.0.0.1:7870", db_dir=str(tmp_path))
        b = LocalScheduler("agentA", invoke_url="http://127.0.0.1:7871", db_dir=str(tmp_path))
        assert a.path == b.path  # same jobs.db (same agent + dir)
        await a.start()
        await b.start()  # interlock: b must NOT start a poll task
        started = (a._task is not None, b._task is not None)
        await a.stop()
        await b.stop()
        # After A releases, a fresh scheduler can claim it.
        c = LocalScheduler("agentA", invoke_url="http://127.0.0.1:7872", db_dir=str(tmp_path))
        await c.start()
        c_started = c._task is not None
        await c.stop()
        return started, c_started

    (a_started, b_started), c_started = asyncio.run(run())
    assert a_started is True
    assert b_started is False
    assert c_started is True
