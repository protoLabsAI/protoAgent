"""Agent-global notes workspace storage for the React operator console.

One persistent, instance-scoped notes workspace the agent and the console
share — *not* per-project. There's no ``.automaker/notes/`` inside project
directories anymore (it was confusing to scatter the agent's notes across
whatever directory happened to be "the project"); the agent has a single
notebook, the same one the console's Notes panel shows. This mirrors the
in-process ``BeadsStore`` (``beads/store.py``). The project / allowed-dirs
list is purely the filesystem security fence, not a notes scope.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

from paths import scope_leaf

DEFAULT_NOTES_PATH = "/sandbox/notes/workspace.json"


def _resolve_notes_path(path: str | None) -> Path:
    """``NOTES_PATH`` env → constructor arg → default. Falls back from a
    non-writable ``/sandbox`` to ``~/.protoagent`` for local dev, then
    instance-scoped — the same shape as the beads + knowledge stores."""
    raw = os.environ.get("NOTES_PATH") or path or DEFAULT_NOTES_PATH
    p = Path(raw).expanduser()
    if str(p).startswith("/sandbox") and not Path("/sandbox").is_dir():
        p = Path.home() / ".protoagent" / "notes" / "workspace.json"
    return scope_leaf(p)


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
    """Load/save the agent's single persistent notes workspace.

    Instance-scoped (one notebook per agent), not per-project. ``allowed_dirs``
    is accepted and ignored for backward compatibility with older call sites —
    notes no longer live inside a project directory, so there's nothing to
    fence here.
    """

    def __init__(self, *, path: str | None = None, allowed_dirs: Any = None):
        self.path = _resolve_notes_path(path)

    def workspace_path(self) -> Path:
        return self.path

    def load_workspace(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else create_default_workspace()
        except (OSError, json.JSONDecodeError, ValueError):
            return create_default_workspace()

    def save_workspace(self, workspace: dict[str, Any]) -> None:
        if not isinstance(workspace, dict):
            raise ValueError("workspace must be an object")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(workspace, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)
