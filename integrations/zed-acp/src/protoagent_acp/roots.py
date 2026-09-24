"""Project name → absolute root, so a ``read_file(project, path)`` becomes a location
Zed can follow.

protoAgent's fs tools address files as ``project`` + a path relative to that project's
root; ACP ``locations`` must be absolute. The map is assembled from, in precedence order:

1. ``--root name=/abs/path`` overrides (the only correct source for a REMOTE instance,
   whose roots are paths on another machine — map them to your local checkout);
2. ``GET /api/fs/roots`` → ``{"roots": {name: abs}}`` — the purpose-built endpoint
   (being added in core; absent on older instances, so a 404 is normal);
3. ``GET /api/config`` → ``filesystem.projects`` (explicit fence roots; these SHADOW the
   registry when set, which is why ``/api/projects`` alone is not enough) and
   ``GET /api/projects`` (the ADR 0095 registry rows);
4. learned at runtime from a ``list_projects`` tool result the agent happened to run
   (``- name  [mode]  /abs/path`` lines).

A path is only emitted when it stays inside its root, so a ``../`` argument can never
make the editor jump outside the project.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import PurePosixPath
from typing import Any

log = logging.getLogger(__name__)

_LIST_PROJECTS_LINE = re.compile(r"^-\s+(?P<name>\S+)\s+\[[^\]]*\]\s+(?P<path>/.+?)\s*$")


def _dig(obj: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


class RootMap:
    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self._overrides = {k: os.path.expanduser(v) for k, v in (overrides or {}).items()}
        self._discovered: dict[str, str] = {}
        self.source = "none"

    @property
    def roots(self) -> dict[str, str]:
        return {**self._discovered, **self._overrides}

    def add(self, name: str, path: str, *, override: bool = False) -> None:
        if not name or not path or not path.startswith("/"):
            return
        if override:
            self._overrides[name] = path
        elif name not in self._discovered:  # first source wins (precedence order above)
            self._discovered[name] = path

    async def load(self, client: Any) -> None:
        """Best-effort discovery over the operator API; never raises."""
        body = await client.get_json("/api/fs/roots")
        if isinstance(body, dict) and isinstance(body.get("roots"), dict):
            for name, path in body["roots"].items():
                self.add(str(name), str(path))
            self.source = "/api/fs/roots"
            return
        cfg = await client.get_json("/api/config")
        fs = _dig(cfg, "config", "filesystem")
        reg = await client.get_json("/api/projects")
        for rows in (_dig(fs, "projects"), _dig(reg, "projects")):
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    self.add(str(row.get("name") or ""), str(row.get("path") or ""))
        if self._discovered:
            self.source = "/api/config + /api/projects"

    def learn_from_list_projects(self, output: Any) -> None:
        for line in str(output or "").splitlines():
            m = _LIST_PROJECTS_LINE.match(line.strip())
            if m:
                self.add(m.group("name"), m.group("path"))

    def resolve(self, project: str | None, rel: str | None) -> str | None:
        """Absolute path for ``rel`` inside ``project`` — ``None`` when the project is
        unknown or the path escapes its root. With exactly one known root, a missing
        ``project`` resolves against it."""
        roots = self.roots
        root = roots.get(project or "") if project else (next(iter(roots.values())) if len(roots) == 1 else None)
        if not root:
            return None
        rel = (rel or ".").strip()
        if rel.startswith("/"):
            candidate = PurePosixPath(rel)
        else:
            candidate = PurePosixPath(root) / rel
        # Lexical normalisation only — the file may live on another machine.
        parts: list[str] = []
        for part in candidate.parts:
            if part == "..":
                if len(parts) > 1:
                    parts.pop()
            elif part != ".":
                parts.append(part)
        normalized = str(PurePosixPath(*parts)) if parts else "/"
        root_n = str(PurePosixPath(root))
        if normalized != root_n and not normalized.startswith(root_n.rstrip("/") + "/"):
            return None
        return normalized

    def project_for(self, path: str) -> tuple[str, str] | None:
        """Which project contains ``path`` (longest root wins) → ``(name, root)``."""
        best: tuple[str, str] | None = None
        for name, root in self.roots.items():
            r = root.rstrip("/")
            if path == r or path.startswith(r + "/"):
                if best is None or len(r) > len(best[1]):
                    best = (name, r)
        return best
