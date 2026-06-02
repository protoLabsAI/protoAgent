from __future__ import annotations

from operator_api.notes import NotesService


def test_notes_service_returns_default_workspace_when_empty(tmp_path) -> None:
    workspace = NotesService(path=str(tmp_path / "workspace.json")).load_workspace()

    assert workspace["version"] == 1
    assert workspace["activeTabId"] in workspace["tabs"]
    assert workspace["tabOrder"] == [workspace["activeTabId"]]


def test_notes_service_saves_and_loads_workspace(tmp_path) -> None:
    # Agent-global notebook: one instance-scoped workspace, no project_path.
    service = NotesService(path=str(tmp_path / "notes" / "workspace.json"))
    workspace = {
        "version": 1,
        "workspaceVersion": 2,
        "activeTabId": "tab-1",
        "tabOrder": ["tab-1"],
        "tabs": {
            "tab-1": {
                "id": "tab-1",
                "name": "Plan",
                "content": "ship it",
                "permissions": {"agentRead": True, "agentWrite": False},
                "metadata": {"createdAt": 1, "updatedAt": 2},
            },
        },
    }

    service.save_workspace(workspace)

    assert service.load_workspace() == workspace
    assert service.workspace_path().exists()
