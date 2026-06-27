"""Tests for the Activity thread wiring (ADR 0003 slice 2)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from a2a_impl import executor
from a2a_impl.executor import TurnOutcome
from operator_api.routes import register_operator_routes


def test_notify_terminal_invokes_hook_and_is_exception_safe():
    outcome = TurnOutcome(
        task_id="t1",
        context_id="system:activity",
        state="completed",
        text="hi",
    )
    seen = []
    prior = executor._ON_TERMINAL[0]
    try:
        executor.set_terminal_hook(seen.append)
        executor._notify_terminal(outcome)
        assert seen == [outcome]

        # A throwing hook must not propagate into the executor.
        def boom(_):
            raise RuntimeError("nope")

        executor.set_terminal_hook(boom)
        executor._notify_terminal(outcome)  # no raise

        # No hook registered → no-op.
        executor.set_terminal_hook(None)
        executor._notify_terminal(outcome)
    finally:
        executor._ON_TERMINAL[0] = prior


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


# ── ActivityLog.prune ────────────────────────────────────────────────────────

from datetime import UTC, datetime

from activity.store import ActivityLog


def _activity_log(tmp_path):
    return ActivityLog(str(tmp_path / "activity.db"))


def test_prune_activity_removes_old(tmp_path):
    """Entries older than keep_days are deleted; recent ones survive."""
    al = _activity_log(tmp_path)
    now = datetime(2024, 3, 1, tzinfo=UTC)
    old = datetime(2024, 1, 1, tzinfo=UTC)

    # Directly insert rows with controlled timestamps via raw SQL so we
    # can set created_at to an old date (ActivityLog.add always uses _now_iso).
    import sqlite3

    db = sqlite3.connect(str(tmp_path / "activity.db"))
    db.execute(
        "INSERT INTO activity (created_at, context_id, origin, text) VALUES (?, ?, ?, ?)",
        (old.isoformat(), "ctx-old", "test", "old entry"),
    )
    db.execute(
        "INSERT INTO activity (created_at, context_id, origin, text) VALUES (?, ?, ?, ?)",
        (now.isoformat(), "ctx-new", "test", "recent entry"),
    )
    db.commit()
    db.close()

    removed = al.prune(keep_days=30, now=now)
    assert removed == 1
    remaining = al.recent(limit=10)
    assert len(remaining) == 1
    assert remaining[0]["text"] == "recent entry"


def test_activity_add_round_trips_stimulus(tmp_path):
    """The stimulus (triggering input) is stored + returned so the feed can show the
    response as an explicit reply to it (#1375)."""
    al = _activity_log(tmp_path)
    al.add(
        context_id="system:activity",
        origin="inbox",
        trigger="ci",
        priority="now",
        text="Build failed on main — investigating.",
        stimulus="CI webhook: build #4821 failed on main.",
    )
    row = al.recent(limit=1)[0]
    assert row["stimulus"] == "CI webhook: build #4821 failed on main."
    # Omitted stimulus is fine (empty), not an error.
    al.add(context_id="system:activity", origin="operator", text="a manual note")
    assert al.recent(limit=1)[0]["stimulus"] in ("", None)


def test_activity_migrates_pre_stimulus_db(tmp_path):
    """A DB created before the `stimulus` column gets it added on open (additive ALTER),
    and existing rows read back with stimulus = None."""
    import sqlite3

    path = str(tmp_path / "activity.db")
    db = sqlite3.connect(path)
    # The original (pre-#1375) schema — no `stimulus` column.
    db.execute(
        "CREATE TABLE activity (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
        "context_id TEXT NOT NULL, origin TEXT NOT NULL DEFAULT '', trigger TEXT, priority TEXT, "
        "state TEXT, text TEXT NOT NULL, task_id TEXT)"
    )
    db.execute(
        "INSERT INTO activity (created_at, context_id, origin, text) VALUES (?, ?, ?, ?)",
        (datetime(2026, 1, 1, tzinfo=UTC).isoformat(), "ctx", "scheduler", "old entry"),
    )
    db.commit()
    db.close()

    al = ActivityLog(path)  # opening migrates the schema
    al.add(context_id="ctx", origin="inbox", text="new entry", stimulus="ping")
    rows = {r["text"]: r for r in al.recent(limit=10)}
    assert rows["old entry"]["stimulus"] is None  # legacy row, back-filled column
    assert rows["new entry"]["stimulus"] == "ping"


def test_prune_activity_keep_all_zero(tmp_path):
    """keep_days=0 removes nothing (keep forever)."""
    al = _activity_log(tmp_path)

    import sqlite3

    db = sqlite3.connect(str(tmp_path / "activity.db"))
    db.execute(
        "INSERT INTO activity (created_at, context_id, origin, text) VALUES (?, ?, ?, ?)",
        (datetime(2020, 1, 1, tzinfo=UTC).isoformat(), "ctx", "test", "ancient"),
    )
    db.commit()
    db.close()

    removed = al.prune(keep_days=0, now=datetime(2026, 1, 1, tzinfo=UTC))
    assert removed == 0
    assert len(al.recent(limit=10)) == 1
