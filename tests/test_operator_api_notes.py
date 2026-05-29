from __future__ import annotations

from operator_api.notes import NotesService


def test_notes_service_returns_default_workspace_for_new_project(tmp_path) -> None:
    workspace = NotesService().load_workspace(str(tmp_path))

    assert workspace["version"] == 1
    assert workspace["activeTabId"] in workspace["tabs"]
    assert workspace["tabOrder"] == [workspace["activeTabId"]]


def test_notes_service_saves_and_loads_workspace(tmp_path) -> None:
    service = NotesService()
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

    service.save_workspace(str(tmp_path), workspace)

    assert service.load_workspace(str(tmp_path)) == workspace
    assert service.workspace_path(str(tmp_path)).exists()
