from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.routes import register_operator_routes


class _FakeTaskStore:
    """The in-process task store the routes' adapter wraps — agent-global, no
    project scope (so project_path is ignored)."""

    def list(self, include_closed: bool = True):
        return [{"id": "task-1", "title": "x", "status": "open"}]

    def create(self, title, *, description="", priority=2, issue_type="task", assignee=""):
        return {"id": "task-2", "title": title, "issue_type": issue_type, "priority": priority}

    def update(self, issue_id, **fields):
        return {"id": issue_id, **fields}

    def close(self, issue_id, reason=None):
        return {"id": issue_id, "status": "closed", "close_reason": reason}

    def delete(self, issue_id):
        return True


def _client(*, run=None):
    app = FastAPI()

    async def default_run(req):
        return f"ran:{req['type']}:{req['prompt']}"

    async def batch(req):
        return f"batch:{len(req['tasks'])}"

    register_operator_routes(
        app,
        runtime_status=lambda: {"graph_loaded": True},
        subagent_list=lambda: [{"name": "researcher"}],
        subagent_run=run or default_run,
        subagent_batch=batch,
        tasks_store=_FakeTaskStore(),
    )
    return TestClient(app)


def test_tasks_store_route_ignores_project_path() -> None:
    """The in-process store adapter ignores project_path — tasks endpoints don't
    require one (the board is agent-global)."""
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda req: "",
        subagent_batch=lambda req: "",
        tasks_store=_FakeTaskStore(),
    )
    client = TestClient(app)
    # no project_path supplied — must not 400
    assert client.get("/api/tasks/status").json() == {"initialized": True}
    assert client.get("/api/tasks/issues").status_code == 200


def test_runtime_status_accepts_async_accessor() -> None:
    """The real console handler is async (#875 offloads the per-poll `ps`
    co-location probe off the loop); the route must await a coroutine accessor
    while still accepting a plain-dict (sync) one for forks/test doubles."""
    app = FastAPI()

    async def _async_status():
        return {"graph_loaded": True, "async": True}

    register_operator_routes(
        app,
        runtime_status=_async_status,
        subagent_list=lambda: [],
        subagent_run=lambda req: "",
        subagent_batch=lambda req: "",
        tasks_store=_FakeTaskStore(),
    )
    assert TestClient(app).get("/api/runtime/status").json() == {"graph_loaded": True, "async": True}


def test_operator_routes_return_expected_shapes(tmp_path) -> None:
    client = _client()

    assert client.get("/api/runtime/status").json() == {"graph_loaded": True}
    assert client.get("/api/subagents").json() == {"subagents": [{"name": "researcher"}]}

    run = client.post(
        "/api/subagents/run",
        json={"type": "researcher", "prompt": "check"},
    )
    assert run.status_code == 200
    assert run.json()["output"] == "ran:researcher:check"

    batch = client.post(
        "/api/subagents/batch",
        json={"tasks": [{"prompt": "one"}, {"prompt": "two"}]},
    )
    assert batch.json()["output"] == "batch:2"

    # The in-process adapter ignores project_path (the board is agent-global).
    assert client.get("/api/tasks/status").json() == {"initialized": True}
    assert (
        client.post("/api/tasks/issues", json={"title": "Task"}).json()["issue"]["id"] == "task-2"
    )
    assert client.patch(
        "/api/tasks/issues/task-1",
        json={"status": "in_progress"},
    ).json()["issue"] == {"id": "task-1", "status": "in_progress"}
    assert client.post(
        "/api/tasks/issues/task-1/close",
        json={"reason": "done"},
    ).json()["issue"] == {"id": "task-1", "status": "closed", "close_reason": "done"}
    assert client.delete("/api/tasks/issues/task-1").json() == {"deleted": True}


def test_operator_routes_map_value_errors_to_400() -> None:
    async def run(_req):
        raise ValueError("bad prompt")

    client = _client(run=run)
    response = client.post(
        "/api/subagents/run",
        json={"type": "researcher", "prompt": "check"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "bad prompt"


# ── goals routes (list + clear) ──────────────────────────────────────────────


def _goals_client(*, goals=None, on_clear=None):
    app = FastAPI()

    async def glist():
        return {"goals": goals if goals is not None else [], "enabled": True}

    async def gclear(session_id):
        if on_clear:
            on_clear(session_id)
        return {"cleared": True}

    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        goal_list=glist,
        goal_clear=gclear,
    )
    return TestClient(app)


def test_goals_list_and_clear() -> None:
    seen = {}
    client = _goals_client(
        goals=[{"session_id": "s1", "condition": "ship it", "status": "active", "iteration": 2}],
        on_clear=lambda sid: seen.update(id=sid),
    )
    body = client.get("/api/goals").json()
    assert body["enabled"] is True
    assert body["goals"][0]["session_id"] == "s1" and body["goals"][0]["status"] == "active"

    assert client.delete("/api/goals/s1").json() == {"cleared": True}
    assert seen["id"] == "s1"


def test_goals_routes_absent_when_not_wired() -> None:
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
    )
    assert TestClient(app).get("/api/goals").status_code == 404


# ── slash commands ───────────────────────────────────────────────────────────


def test_chat_commands_endpoint() -> None:
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
        chat_commands=lambda: {"commands": [{"name": "goal", "description": "set a goal", "usage": "/goal ..."}]},
    )
    body = TestClient(app).get("/api/chat/commands").json()
    assert body["commands"][0]["name"] == "goal"


def test_chat_commands_absent_when_not_wired() -> None:
    app = FastAPI()
    register_operator_routes(
        app,
        runtime_status=lambda: {},
        subagent_list=lambda: [],
        subagent_run=lambda r: None,
        subagent_batch=lambda r: None,
    )
    assert TestClient(app).get("/api/chat/commands").status_code == 404


def test_workflows_plugin_router_save_validates_and_deletes(tmp_path, monkeypatch) -> None:
    """The workflows plugin self-registers /api/plugins/workflows; save validates the
    recipe (against the live subagent registry) then writes it; an unknown-subagent
    recipe is rejected (400); DELETE removes it. Workflows now live in the plugin."""
    from types import SimpleNamespace

    import plugins.workflows as wf
    import runtime.state as rs

    # Point the writable recipe dir at a tmp dir + a known subagent set (no live STATE).
    monkeypatch.setattr(wf.sdk, "config", lambda: SimpleNamespace(workflow_dir=str(tmp_path)))
    monkeypatch.setattr(wf.sdk, "subagent_types", lambda: {"researcher"})
    # register() publishes onto global STATE — record so monkeypatch restores it (no leak).
    monkeypatch.setattr(rs.STATE, "workflow_registry", None, raising=False)
    monkeypatch.setattr(rs.STATE, "workflow_run", None, raising=False)

    captured: dict = {}

    class _Reg:
        workflow_dirs: list = []

        def register_tools(self, tools):
            pass

        def register_workflow_dir(self, d):
            pass

        def register_router(self, router, prefix=None):
            captured["router"], captured["prefix"] = router, prefix

    wf.register(_Reg())

    app = FastAPI()
    app.include_router(captured["router"], prefix=captured["prefix"])
    client = TestClient(app)

    good = {
        "name": "demo",
        "inputs": [{"name": "topic", "required": True}],
        "steps": [{"id": "s1", "subagent": "researcher", "prompt": "{{inputs.topic}}"}],
        "output": "{{steps.s1.output}}",
    }
    r = client.post("/api/plugins/workflows/save", json=good)
    assert r.status_code == 200 and r.json()["saved"] is True

    bad = dict(good, name="bad", steps=[{"id": "s1", "subagent": "ghost", "prompt": "x"}])
    assert client.post("/api/plugins/workflows/save", json=bad).status_code == 400

    assert client.delete("/api/plugins/workflows/demo").json()["deleted"] is True
    assert client.delete("/api/plugins/workflows/demo").json()["deleted"] is False
