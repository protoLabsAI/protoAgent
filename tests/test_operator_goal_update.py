"""PATCH /api/goals/{session_id} — edit a goal's contract + budgets in place (#4955 Q9).

The route (operator_api/routes.py) is a thin wrapper over ``_operator_goals_update``
(operator_api/console_handlers.py). These drive the whole stack through a TestClient with a
real ``GoalController`` + ``GoalStore`` on a tmp dir, so the status-code mapping, the store
write and its ``goal.changed`` publish are all exercised together.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from graph.goals.store import GoalStore
from graph.goals.types import GoalState
from operator_api.routes import register_operator_routes


def _client(monkeypatch, tmp_path, *, controller=True):
    import runtime.state as rs

    monkeypatch.delenv("A2A_AUTH_TOKEN", raising=False)
    ctrl = None
    if controller:
        from graph.goals.controller import GoalController

        ctrl = GoalController(config=None, store=GoalStore(base_dir=str(tmp_path)))
    monkeypatch.setattr(rs.STATE, "goal_controller", ctrl, raising=False)

    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
    )
    return TestClient(app), ctrl


def _seed(ctrl, **kw):
    """Persist a goal with sensible defaults; keyword args override the dataclass fields."""
    state = GoalState(
        session_id=kw.pop("session_id", "s1"),
        condition=kw.pop("condition", "ship it"),
        verifier=kw.pop("verifier", {"type": "command", "command": "pytest -q"}),
        **kw,
    )
    ctrl.store.set(state)
    return state


def test_patch_updates_named_fields_and_fires_event(monkeypatch, tmp_path):
    # r1: a partial body changes ONLY the named fields; the response + stored goal show
    # them and goal.changed fires. Untouched fields keep their values.
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1", outcome="old", stop_when="old stop", constraints=["keep tests green"])

    from graph.plugins import host

    events: list[tuple] = []
    monkeypatch.setattr(host.HOST, "publish", lambda topic, data: events.append((topic, data)))

    resp = client.patch("/api/goals/s1", json={"outcome": "suite green", "stop_when": "needs prod access"})
    assert resp.status_code == 200
    goal = resp.json()["goal"]
    assert goal["outcome"] == "suite green"
    assert goal["stop_when"] == "needs prod access"
    # Fields the body didn't name are untouched.
    assert goal["condition"] == "ship it"
    assert goal["constraints"] == ["keep tests green"]
    assert goal["verifier"] == {"type": "command", "command": "pytest -q"}

    stored = ctrl.store.get("s1")
    assert stored.outcome == "suite green" and stored.stop_when == "needs prod access"
    assert [t for t, _ in events] == ["goal.changed"]
    assert events[0][1]["session_id"] == "s1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("condition", "a different condition"),
        ("verifier", {"type": "llm"}),
        ("status", "achieved"),
        ("history", [{"iteration": 1, "status": "continue"}]),
    ],
)
def test_patch_rejects_immutable_fields(monkeypatch, tmp_path, field, value):
    # r2: naming the condition, verifier, status or history is a 400 and changes nothing.
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1")
    resp = client.patch("/api/goals/s1", json={field: value})
    assert resp.status_code == 400
    stored = ctrl.store.get("s1")
    assert stored.condition == "ship it"
    assert stored.status == "active"
    assert stored.verifier == {"type": "command", "command": "pytest -q"}
    assert stored.history == []


def test_patch_rejects_unknown_key(monkeypatch, tmp_path):
    # The contract is strict: any key outside the editable set (here a drive-loop counter)
    # is a 400, not a silent no-op — a typo'd field name never half-applies.
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1")
    resp = client.patch("/api/goals/s1", json={"iteration": 99})
    assert resp.status_code == 400
    assert ctrl.store.get("s1").iteration == 0


def test_patch_unknown_session_404(monkeypatch, tmp_path):
    # r3: an unknown session is a 404.
    client, _ = _client(monkeypatch, tmp_path)
    assert client.patch("/api/goals/ghost", json={"outcome": "x"}).status_code == 404


def test_patch_max_iterations_below_current_iteration_400(monkeypatch, tmp_path):
    # r3: a max_iterations below the goal's current iteration would exhaust it immediately,
    # so it's a 400 and the budget is left alone. Equal-or-above is fine.
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1", iteration=5, max_iterations=8)

    low = client.patch("/api/goals/s1", json={"max_iterations": 3})
    assert low.status_code == 400
    assert ctrl.store.get("s1").max_iterations == 8

    ok = client.patch("/api/goals/s1", json={"max_iterations": 20})
    assert ok.status_code == 200
    assert ctrl.store.get("s1").max_iterations == 20


@pytest.mark.parametrize(
    "body",
    [
        {"max_iterations": 0},
        {"max_iterations": 10_000},
        {"max_iterations": "lots"},
        {"no_progress_limit": 0},
        {"no_progress_limit": 10_000},
    ],
)
def test_patch_rejects_out_of_range_budgets(monkeypatch, tmp_path, body):
    # Budgets reuse the goal-set bounds (a whole number in 1..MAX); out of range is a 400.
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1")
    assert client.patch("/api/goals/s1", json=body).status_code == 400


def test_patch_coerces_string_list_fields(monkeypatch, tmp_path):
    # Parity with the goal-set handler: a bare string for constraints/boundaries becomes a
    # 1-element list.
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1")
    resp = client.patch("/api/goals/s1", json={"constraints": "no new deps", "boundaries": "graph/"})
    assert resp.status_code == 200
    stored = ctrl.store.get("s1")
    assert stored.constraints == ["no new deps"]
    assert stored.boundaries == ["graph/"]


def test_patch_updates_no_progress_limit(monkeypatch, tmp_path):
    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, session_id="s1")
    assert ctrl.store.get("s1").no_progress_limit is None
    resp = client.patch("/api/goals/s1", json={"no_progress_limit": 4})
    assert resp.status_code == 200
    assert resp.json()["goal"]["no_progress_limit"] == 4
    assert ctrl.store.get("s1").no_progress_limit == 4


def test_patch_goals_disabled_503(monkeypatch, tmp_path):
    # Goal mode off (no controller) is a 503 — the capability is unavailable, not the goal
    # missing.
    client, _ = _client(monkeypatch, tmp_path, controller=False)
    assert client.patch("/api/goals/s1", json={"outcome": "x"}).status_code == 503
