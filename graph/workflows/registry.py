"""Workflow registry — load declarative recipes (`*.yaml`) from disk.

Scans one or more directories for workflow recipes (bundled examples in the
repo's ``workflows/`` dir + a writable dir for user/agent-emitted ones, same
shape as the skills loader). A recipe's ``name`` is the lookup key; later dirs
win on a name clash so a user copy can override a bundled example.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class WorkflowRegistry:
    def __init__(self, dirs: list[str] | None = None):
        self._dirs = [Path(d) for d in (dirs or [])]
        self._recipes: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        recipes: dict[str, dict[str, Any]] = {}
        for d in self._dirs:
            if not d.is_dir():
                continue
            for path in sorted(d.glob("*.yaml")) + sorted(d.glob("*.yml")):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError) as exc:
                    log.warning("[workflows] skipping %s: %s", path, exc)
                    continue
                if isinstance(data, dict) and isinstance(data.get("name"), str):
                    recipes[data["name"]] = data
                else:
                    log.warning("[workflows] skipping %s: not a named recipe", path)
        self._recipes = recipes
        log.info("[workflows] loaded %d workflow(s): %s", len(recipes), ", ".join(sorted(recipes)) or "(none)")

    def list(self) -> list[dict[str, Any]]:
        """Lightweight summaries for the UI / tool discovery."""
        out = []
        for name, r in sorted(self._recipes.items()):
            out.append({
                "name": name,
                "description": r.get("description", ""),
                "inputs": [
                    {"name": i.get("name"), "required": bool(i.get("required")), "default": i.get("default")}
                    for i in (r.get("inputs") or []) if isinstance(i, dict)
                ],
                "steps": [
                    {"id": s.get("id"), "subagent": s.get("subagent"), "depends_on": list(s.get("depends_on", []) or [])}
                    for s in (r.get("steps") or []) if isinstance(s, dict)
                ],
            })
        return out

    def get(self, name: str) -> dict[str, Any] | None:
        return self._recipes.get(name)

    def names(self) -> list[str]:
        return sorted(self._recipes)
