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

pytestmark = pytest.mark.platform_sensitive


def _replace_error(winerror):
    error = PermissionError("replacement denied")
    if winerror is not None:
        error.winerror = winerror
    return error


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


@pytest.mark.asyncio
@pytest.mark.parametrize("met", [False, True])
async def test_patch_during_verifier_preserves_edit_and_progress(monkeypatch, tmp_path, met):
    import asyncio

    from graph.goals import controller
    from graph.goals.types import VerifyResult
    from operator_api.console_handlers import _operator_goals_update

    _, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, max_iterations=1)
    entered, release = asyncio.Event(), asyncio.Event()

    async def verify(*args):
        entered.set()
        await release.wait()
        return VerifyResult(met, "verified", "evidence")

    monkeypatch.setattr(controller, "run_verifier", verify)
    monkeypatch.setattr("graph.self_improvement.schedule_review", lambda *args: None)
    task = asyncio.create_task(ctrl.evaluate("s1", last_text="done"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await _operator_goals_update(
            "s1", {"outcome": "edited", "stop_when": "approval needed", "max_iterations": 10}
        )
        assert response["goal"]["outcome"] == "edited"
    finally:
        release.set()
    decision = await asyncio.wait_for(task, 5)
    stored = ctrl.store.get("s1")
    assert stored.outcome == "edited"
    assert stored.stop_when == "approval needed"
    assert stored.max_iterations == 10
    assert stored.status == ("achieved" if met else "active")
    assert stored.iteration == (0 if met else 1)
    assert len(stored.history) == 1
    assert decision.action == ("done" if met else "continue")
    if not met:
        assert "edited" in decision.message


@pytest.mark.asyncio
@pytest.mark.parametrize("winerror", [None, 5, 32, 33])
async def test_patch_waiting_for_completion_preserves_terminal_state(monkeypatch, tmp_path, winerror):
    import asyncio
    import json
    import threading

    from graph.goals import store
    from graph.goals.types import VerifyResult
    from operator_api.console_handlers import _operator_goals_update

    _, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl)
    original_replace = store.os.replace
    attempts = {}

    def contended_replace(source, destination):
        # Fail both the edit and terminal write twice, then perform a real swap.
        with open(source, encoding="utf-8") as fh:
            status = json.load(fh)["status"]
        attempts[status] = attempts.get(status, 0) + 1
        if winerror is not None and attempts[status] <= 2:
            raise _replace_error(winerror)
        original_replace(source, destination)

    monkeypatch.setattr(store.os, "replace", contended_replace)
    entered, release = threading.Event(), threading.Event()
    original_get = ctrl.store.get

    def paused_get(session_id):
        state = original_get(session_id)
        if threading.current_thread() is not threading.main_thread() and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return state

    monkeypatch.setattr(ctrl.store, "get", paused_get)
    monkeypatch.setattr("graph.self_improvement.schedule_review", lambda *args: None)
    patch = asyncio.create_task(_operator_goals_update("s1", {"outcome": "edited"}))
    assert await asyncio.to_thread(entered.wait, 5)

    # Complete on a separate loop/thread while PATCH holds its read/modify/write
    # transaction. Without that transaction PATCH can restore the old active state.
    started = threading.Event()

    def complete():
        started.set()
        return asyncio.run(ctrl.finish_mid_turn("s1", VerifyResult(True, "passed", "proof")))

    completion = asyncio.create_task(asyncio.to_thread(complete))
    try:
        assert await asyncio.to_thread(started.wait, 5)
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(patch, completion), 5)
    stored = ctrl.store.get("s1")
    assert stored.status == "achieved"
    assert stored.finished_at is not None
    assert stored.history[-1]["status"] == "achieved"
    assert stored.outcome == "edited"
    assert attempts == dict.fromkeys(("active", "achieved"), 1 if winerror is None else 3)
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("winerror", [None, 5, 32, 33, 112])
def test_patch_failed_replace_reports_failure_and_preserves_goal(monkeypatch, tmp_path, winerror):
    from graph.goals import store
    from graph.plugins import host

    client, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl, outcome="old", status="achieved", finished_at=123)
    before = ctrl.store.get("s1").to_dict()
    events, sleeps, attempts = [], [], []
    error = _replace_error(winerror)

    def denied_replace(source, destination):
        attempts.append(source)
        raise error

    monkeypatch.setattr(store.os, "replace", denied_replace)
    monkeypatch.setattr(store.time, "sleep", sleeps.append)
    monkeypatch.setattr(host.HOST, "publish", lambda *args: events.append(args))
    response = client.patch("/api/goals/s1", json={"outcome": "edited"})
    assert response.status_code == 500
    assert ctrl.store.get("s1").to_dict() == before
    assert events == []
    assert len(attempts) == (6 if winerror in (5, 32, 33) else 1)
    assert sleeps == ([0.01, 0.02, 0.04, 0.08, 0.16] if winerror in (5, 32, 33) else [])
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("reader", ["get", "all"])
def test_goal_readers_close_file_before_replacement(monkeypatch, tmp_path, reader):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from graph.goals import store

    _, ctrl = _client(monkeypatch, tmp_path)
    state = _seed(ctrl)
    state.outcome = "edited"
    reading, release, writing, replaced = (threading.Event() for _ in range(4))
    original_load, original_replace = store.json.load, store.os.replace

    def paused_load(fh):
        reading.set()
        assert release.wait(5)
        return original_load(fh)

    def replace(source, destination):
        replaced.set()
        return original_replace(source, destination)

    def write():
        writing.set()
        ctrl.store.set(state)

    monkeypatch.setattr(store.json, "load", paused_load)
    monkeypatch.setattr(store.os, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        read = pool.submit(ctrl.store.get, "s1") if reader == "get" else pool.submit(ctrl.store.all)
        try:
            assert reading.wait(5)
            written = pool.submit(write)
            assert writing.wait(5)
            assert not replaced.wait(0.1)
        finally:
            release.set()
        read.result(timeout=5)
        written.result(timeout=5)
    assert replaced.is_set()
    assert ctrl.store.get("s1").outcome == "edited"


@pytest.mark.asyncio
async def test_patch_during_finish_hooks_keeps_achieved_state(monkeypatch, tmp_path):
    import asyncio

    from graph.goals.types import VerifyResult
    from operator_api.console_handlers import _operator_goals_update

    _, ctrl = _client(monkeypatch, tmp_path)
    _seed(ctrl)
    entered, release = asyncio.Event(), asyncio.Event()

    async def hooks(*args):
        entered.set()
        await release.wait()

    monkeypatch.setattr("graph.goals.hooks.fire_goal_hooks", hooks)
    monkeypatch.setattr("graph.self_improvement.schedule_review", lambda *args: None)
    completion = asyncio.create_task(ctrl.finish_mid_turn("s1", VerifyResult(True, "passed", "proof")))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Hooks may themselves trigger goal operations. They must not hold the
        # store transaction, and a PATCH here must retain the terminal history.
        response = await asyncio.wait_for(_operator_goals_update("s1", {"outcome": "edited"}), 5)
        assert response["goal"]["status"] == "achieved"
        assert response["goal"]["history"][-1]["status"] == "achieved"
    finally:
        release.set()
    await asyncio.wait_for(completion, 5)
    assert ctrl.store.get("s1").outcome == "edited"
    assert ctrl.store.get("s1").status == "achieved"
