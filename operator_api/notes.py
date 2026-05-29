"""Project notes workspace storage for the React operator console."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from operator_api.paths import resolve_project_path


def create_default_workspace() -> dict[str, Any]:
    now = int(time.time() * 1000)
    tab_id = str(uuid.uuid4())
    return {
        "version": 1,
        "workspaceVersion": 0,
        "activeTabId": tab_id,
        "tabOrder": [tab_id],
        "tabs": {
            tab_id: {
                "id": tab_id,
                "name": "Notes",
                "content": "",
                "permissions": {"agentRead": True, "agentWrite": True},
                "metadata": {
                    "createdAt": now,
                    "updatedAt": now,
                    "wordCount": 0,
                    "characterCount": 0,
                },
            },
        },
    }


class NotesService:
    """Load/save a ProtoMaker-compatible notes workspace."""

    def __init__(self, *, allowed_dirs: Callable[[], list[str]] | None = None):
        self._allowed_dirs = allowed_dirs

    def workspace_path(self, project_path: str) -> Path:
        allowed = self._allowed_dirs() if self._allowed_dirs is not None else None
        return resolve_project_path(project_path, allowed) / ".automaker" / "notes" / "workspace.json"

    def load_workspace(self, project_path: str) -> dict[str, Any]:
        path = self.workspace_path(project_path)
        try:
            with path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else create_default_workspace()
        except (OSError, json.JSONDecodeError, ValueError):
            return create_default_workspace()

    def save_workspace(self, project_path: str, workspace: dict[str, Any]) -> None:
        if not isinstance(workspace, dict):
            raise ValueError("workspace must be an object")
        path = self.workspace_path(project_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(workspace, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
