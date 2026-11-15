"""``GET /api/tasks/issues`` tags each ``in_progress`` task ``driven`` | ``idle`` |
``stalled`` (ADR 0129 D9 item 5), computes it once per request against one
``default_sources()``, and never lets that computation fail the list."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.routes import register_operator_routes
from tasks.stall import DriverSources, task_activity


class _FakeTaskStore:
    """A store double returning the exact rows a test hands it — the adapter calls
    ``list()`` with no args and ignores project scope."""

    def __init__(self, rows):
        self._rows = rows

    def list(self, include_closed: bool = True):
        return [dict(r) for r in self._rows]


def _client(rows):
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda req: "",
        subagent_batch=lambda req: "",
        tasks_store=_FakeTaskStore(rows),
    )
    return TestClient(app)


def _sources(**overrides) -> DriverSources:
    """Fake driver signals — every source quiet by default, overridable per test."""
    base = dict(
        list_jobs=lambda: [],
        active_watches=lambda: [],
        running_delegations=lambda s: 0,
        running_background=lambda s: 0,
        turn_running=lambda s: False,
    )
    base.update(overrides)
    return DriverSources(**base)


def _by_id(client):
    return {row["id"]: row for row in client.get("/api/tasks/issues").json()["issues"]}


def test_activity_covers_driven_idle_stalled_and_only_in_progress(monkeypatch):
    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    rows = [
        # driven by an unresolved link — no live source needed, old age notwithstanding.
        {"id": "t-link", "status": "in_progress", "session_id": "",
         "links": [{"ref": "bd-1", "state": "open"}], "updated_at": old},
        # driven by a live turn in its session, again regardless of age.
        {"id": "t-src", "status": "in_progress", "session_id": "sess-live",
         "links": [], "updated_at": old},
        # nothing driving it, but freshly touched → idle.
        {"id": "t-idle", "status": "in_progress", "session_id": "",
         "links": [], "updated_at": now.isoformat()},
        # nothing driving it and untouched past the threshold → stalled.
        {"id": "t-stall", "status": "in_progress", "session_id": "",
         "links": [], "updated_at": old},
        # non-in_progress rows get no activity field at all.
        {"id": "t-open", "status": "open", "updated_at": old},
        {"id": "t-wait", "status": "waiting", "updated_at": old},
        {"id": "t-closed", "status": "closed", "updated_at": old},
    ]
    calls = {"n": 0}

    def _make():
        calls["n"] += 1
        return _sources(turn_running=lambda s: s == "sess-live")

    monkeypatch.setattr("tasks.stall.default_sources", _make)
    issues = _by_id(_client(rows))

    assert issues["t-link"]["activity"] == "driven"
    assert issues["t-src"]["activity"] == "driven"
    assert issues["t-idle"]["activity"] == "idle"
    assert issues["t-stall"]["activity"] == "stalled"
    for tid in ("t-open", "t-wait", "t-closed"):
        assert "activity" not in issues[tid]
    # One default_sources() for the whole request — not re-read per task.
    assert calls["n"] == 1


def test_a_raising_source_is_driven_and_still_returns_the_list(monkeypatch):
    def boom():
        raise RuntimeError("scheduler read failed")

    rows = [{"id": "t-1", "status": "in_progress", "session_id": "sess-1", "links": [],
             "updated_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat()}]
    monkeypatch.setattr("tasks.stall.default_sources", lambda: _sources(list_jobs=boom))

    resp = _client(rows).get("/api/tasks/issues")
    assert resp.status_code == 200
    # Fail-quiet: a source we can't read counts as driving the task (never nudge on
    # missing evidence), and the list comes back either way.
    assert resp.json()["issues"][0]["activity"] == "driven"


def test_default_sources_failure_leaves_the_field_off(monkeypatch):
    def boom():
        raise RuntimeError("runtime not wired")

    rows = [{"id": "t-1", "status": "in_progress", "session_id": "", "links": [],
             "updated_at": datetime.now(UTC).isoformat()}]
    monkeypatch.setattr("tasks.stall.default_sources", boom)

    resp = _client(rows).get("/api/tasks/issues")
    assert resp.status_code == 200
    # Building the sources blew up → no activity on any row and no 500; the console
    # falls back to its prior behaviour.
    assert "activity" not in resp.json()["issues"][0]


def test_unreadable_updated_at_is_idle_not_stalled():
    # task_activity never claims a stall it cannot prove — an unparseable updated_at is idle.
    task = {"id": "t-1", "status": "in_progress", "session_id": "", "links": [],
            "updated_at": "not-a-date"}
    assert task_activity(task, now=datetime.now(UTC), sources=_sources()) == "idle"
