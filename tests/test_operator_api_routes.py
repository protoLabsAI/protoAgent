from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from operator_api.routes import register_operator_routes


class _Notes:
    def __init__(self) -> None:
        self.saved = None

    def load_workspace(self, project_path: str):
        return {"project_path": project_path}

    def save_workspace(self, project_path: str, workspace):
        self.saved = (project_path, workspace)


class _Beads:
    def status(self, project_path: str):
        return {"initialized": True, "project_path": project_path}

    def init(self, project_path: str, prefix=None):
        return {"initialized": True, "prefix": prefix}

    def list(self, project_path: str):
        return [{"id": "bd-1", "project_path": project_path}]

    def create(self, project_path: str, issue):
        return {"id": "bd-2", "title": issue["title"], "project_path": project_path}

    def update(self, project_path: str, issue_id: str, update):
        return {"id": issue_id, "status": update["status"], "project_path": project_path}

    def close(self, project_path: str, issue_id: str, reason=None):
        return {"id": issue_id, "status": "closed", "reason": reason}

    def delete(self, project_path: str, issue_id: str):
        return {"deleted": issue_id, "project_path": project_path}


def _client(*, run=None):
    app = FastAPI()
    notes = _Notes()

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
        notes_service=notes,
        beads_service=_Beads(),
    )
    return TestClient(app), notes


def test_operator_routes_return_expected_shapes(tmp_path) -> None:
    client, notes = _client()

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

    notes_path = str(tmp_path)
    assert client.get("/api/notes/workspace", params={"project_path": notes_path}).json() == {
        "workspace": {"project_path": notes_path},
    }
    save = client.post(
        "/api/notes/workspace",
        json={"project_path": notes_path, "workspace": {"tabs": {}}},
    )
    assert save.json() == {"ok": True}
    assert notes.saved == (notes_path, {"tabs": {}})

    assert client.get("/api/beads/status", params={"project_path": notes_path}).json() == {
        "initialized": True,
        "project_path": notes_path,
    }
    assert client.post(
        "/api/beads/issues",
        json={"project_path": notes_path, "title": "Task"},
    ).json()["issue"]["id"] == "bd-2"
    assert client.patch(
        "/api/beads/issues/bd-1",
        json={"project_path": notes_path, "status": "in_progress"},
    ).json()["issue"] == {"id": "bd-1", "status": "in_progress", "project_path": notes_path}
    assert client.post(
        "/api/beads/issues/bd-1/close",
        json={"project_path": notes_path, "reason": "done"},
    ).json()["issue"] == {"id": "bd-1", "status": "closed", "reason": "done"}
    assert client.delete(
        "/api/beads/issues/bd-1",
        params={"project_path": notes_path},
    ).json() == {"deleted": "bd-1", "project_path": notes_path}


def test_operator_routes_map_value_errors_to_400() -> None:
    async def run(_req):
        raise ValueError("bad prompt")

    client, _notes = _client(run=run)
    response = client.post(
        "/api/subagents/run",
        json={"type": "researcher", "prompt": "check"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "bad prompt"
