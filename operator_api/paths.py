"""Path helpers for operator-console APIs."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def _resolved_roots(allowed_dirs: Iterable[str]) -> list[Path]:
    roots: list[Path] = []
    for raw in allowed_dirs:
        if not raw or not str(raw).strip():
            continue
        roots.append(Path(raw).expanduser().resolve())
    return roots


def resolve_project_path(
    project_path: str,
    allowed_dirs: Iterable[str] | None = None,
) -> Path:
    """Resolve and validate a project directory path from the UI.

    When ``allowed_dirs`` is provided, the resolved path must be equal to
    or nested under one of those directories. This is the operator-console
    sandbox: the React client sends a free-text ``project_path``, so the
    server — not the client — decides which directories tasks/notes may
    touch. ``None`` means "no allowlist configured" and stays permissive
    so non-operator callers and tests keep the old behavior.

    Resolution happens before the containment check (``Path.resolve`` walks
    symlinks and normalizes ``..``), so neither ``../../etc`` nor a symlink
    pointing outside an allowed root can escape the sandbox.
    """
    if not project_path or not project_path.strip():
        raise ValueError("project_path is required")
    path = Path(project_path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"project_path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"project_path is not a directory: {path}")

    if allowed_dirs is not None:
        roots = _resolved_roots(allowed_dirs)
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError(f"project_path is outside the allowed directories: {path}")

    return path
