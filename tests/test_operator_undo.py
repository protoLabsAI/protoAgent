"""Server-side undo for console goal/watch deletes (#4955 Q10).

DELETE /api/goals/{session_id} and DELETE /api/watches/{watch_id} stash a snapshot and
return an ``undo_token``; POST /api/undo/{token} within ``UNDO_WINDOW_S`` writes the record
back through the store's own ``set`` (re-firing its change event). Covers clear → restore
for each kind, expiry via a monkeypatched clock, and the unknown / already-used 404s.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.goals.controller import GoalController
from graph.goals.store import GoalStore
from graph.goals.types import GoalState
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore
from graph.watches.types import Watch
from operator_api import console_handlers as ch
from operator_api.routes import NotFoundError, register_operator_routes
from operator_api.undo import UNDO_WINDOW_S, LEDGER, UndoLedger


@pytest.fixture(autouse=True)
def _clean_ledger():
    """Isolate each test from tokens minted by another."""
    LEDGER._entries.clear()
    yield
    LEDGER._entries.clear()


@pytest.fixture
def events(monkeypatch):
    """Capture the bus events the stores publish (``goal.changed`` / ``watch.changed``)."""
    import graph.plugins.host as host

    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: seen.append((topic, data)), raising=False)
    return seen


def _client(monkeypatch, tmp_path):
    """A TestClient wired to the real goal/watch clear handlers over tmp stores."""
    import runtime.state as rs

    goals_dir = tmp_path / "goals"
    watches_dir = tmp_path / "watches"
    goals_dir.mkdir()
    watches_dir.mkdir()
    goal_store = GoalStore(base_dir=str(goals_dir))
    watch_store = WatchStore(base_dir=str(watches_dir))
    monkeypatch.setattr(rs.STATE, "goal_controller", GoalController(config=None, store=goal_store), raising=False)
    monkeypatch.setattr(rs.STATE, "watch_controller", WatchController(config=None, store=watch_store), raising=False)
    monkeypatch.setattr(rs.STATE, "tasks_store", None, raising=False)

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        goal_list=ch._operator_goals_list,
        goal_clear=ch._operator_goals_clear,
        watch_list=ch._operator_watches_list,
        watch_clear=ch._operator_watches_clear,
    )
    return TestClient(app), goal_store, watch_store


def test_goal_clear_returns_token_and_undo_restores(monkeypatch, tmp_path, events):
    client, goal_store, _ = _client(monkeypatch, tmp_path)
    goal_store.set(
        GoalState(session_id="s1", condition="ship it", verifier={"type": "llm"}, iteration=3, max_iterations=9)
    )

    deleted = client.delete("/api/goals/s1")
    assert deleted.status_code == 200
    token = deleted.json()["undo_token"]
    assert token and goal_store.get("s1") is None  # the delete really happened

    before = len(events)
    undo = client.post(f"/api/undo/{token}")
    assert undo.status_code == 200
    assert undo.json() == {"restored": True, "kind": "goal"}

    # Restored with the same id and fields, and the store's change event fired.
    restored = goal_store.get("s1")
    assert restored is not None
    assert restored.condition == "ship it" and restored.iteration == 3 and restored.max_iterations == 9
    assert ("goal.changed", {"session_id": "s1"}) in events[before:]

    # A second redeem of the same token is a 404 — undo is one-shot (r3).
    assert client.post(f"/api/undo/{token}").status_code == 404


def test_watch_clear_returns_token_and_undo_restores(monkeypatch, tmp_path, events):
    client, _, watch_store = _client(monkeypatch, tmp_path)
    # A terminal, repeating watch with non-default settings — undo must bring back the id,
    # the settings, and the status exactly as they were.
    watch_store.set(
        Watch(
            id="w1",
            condition="deploy finished",
            verifier={"type": "llm"},
            status="met",
            interval_s=120.0,
            repeat=True,
            run_prompt="run the smoke test",
        )
    )

    deleted = client.delete("/api/watches/w1")
    assert deleted.status_code == 200
    token = deleted.json()["undo_token"]
    assert token and watch_store.get("w1") is None

    before = len(events)
    undo = client.post(f"/api/undo/{token}")
    assert undo.json() == {"restored": True, "kind": "watch"}

    restored = watch_store.get("w1")
    assert restored is not None
    assert restored.id == "w1" and restored.status == "met"
    assert restored.interval_s == 120.0 and restored.repeat is True
    assert restored.run_prompt == "run the smoke test"
    assert ("watch.changed", {"id": "w1"}) in events[before:]

    assert client.post(f"/api/undo/{token}").status_code == 404


def test_clearing_a_missing_record_mints_no_token(monkeypatch, tmp_path):
    """A record that doesn't exist keeps the existing not-found behaviour: the clear still
    returns ``{cleared: False}`` with no ``undo_token`` (r1)."""
    client, _, _ = _client(monkeypatch, tmp_path)

    goal = client.delete("/api/goals/nope")
    assert goal.status_code == 200 and goal.json() == {"cleared": False, "tasks_closed": 0}
    assert "undo_token" not in goal.json()

    watch = client.delete("/api/watches/nope")
    assert watch.status_code == 200 and watch.json() == {"cleared": False}
    assert "undo_token" not in watch.json()


def test_unknown_token_is_404(monkeypatch, tmp_path):
    client, _, _ = _client(monkeypatch, tmp_path)
    assert client.post("/api/undo/never-minted-this").status_code == 404


def test_close_tasks_undo_restores_goal_only(monkeypatch, tmp_path):
    """Goals cleared with ``close_tasks=true`` undo the goal but NOT the closed tasks —
    the documented limit."""
    import runtime.state as rs

    client, goal_store, _ = _client(monkeypatch, tmp_path)
    goal_store.set(GoalState(session_id="s1", condition="ship it", verifier={"type": "llm"}))

    class _Tasks:
        def __init__(self):
            self.closed: list[str] = []

        def list(self, *, include_closed=True, session_id=None):
            return [{"id": "t1"}, {"id": "t2"}] if session_id == "s1" and not self.closed else []

        def close(self, issue_id, reason=None):
            self.closed.append(issue_id)

    tasks = _Tasks()
    monkeypatch.setattr(rs.STATE, "tasks_store", tasks, raising=False)

    deleted = client.delete("/api/goals/s1?close_tasks=true")
    assert deleted.json()["tasks_closed"] == 2 and tasks.closed == ["t1", "t2"]
    token = deleted.json()["undo_token"]

    assert client.post(f"/api/undo/{token}").json() == {"restored": True, "kind": "goal"}
    # The goal is back; the tasks it closed stay closed (undo does not reopen them).
    assert goal_store.get("s1") is not None
    assert tasks.closed == ["t1", "t2"]


# --- ledger unit behaviour (expiry via a monkeypatched clock) ----------------


def test_entry_past_the_window_is_a_404_and_restores_nothing():
    clock = {"t": 1000.0}
    ledger = UndoLedger(clock=lambda: clock["t"])
    restored: list[dict] = []
    token = ledger.stash("goal", {"session_id": "s1"}, lambda rec: restored.append(rec))

    clock["t"] += UNDO_WINDOW_S + 0.1  # step the clock past the window
    with pytest.raises(NotFoundError):
        ledger.restore(token)
    assert restored == []  # the stashed restore never ran


def test_stash_snapshots_the_record():
    """The stash is a copy — mutating the caller's dict afterwards can't change what undo
    writes back."""
    ledger = UndoLedger()
    record = {"session_id": "s1", "condition": "ship it"}
    seen: list[dict] = []
    token = ledger.stash("goal", record, lambda rec: seen.append(rec))
    record["condition"] = "mutated after stash"
    ledger.restore(token)
    assert seen == [{"session_id": "s1", "condition": "ship it"}]


def test_restore_runs_once_then_404():
    ledger = UndoLedger()
    count = {"n": 0}
    token = ledger.stash("watch", {"id": "w1"}, lambda rec: count.__setitem__("n", count["n"] + 1))
    assert ledger.restore(token) == "watch"
    assert count["n"] == 1
    with pytest.raises(NotFoundError):
        ledger.restore(token)
    assert count["n"] == 1  # the second redeem did not run the restore again
