"""Path helpers for operator-console APIs."""

from __future__ import annotations

from pathlib import Path


def resolve_project_path(project_path: str) -> Path:
    """Resolve and validate a project directory path from the UI."""
    if not project_path or not project_path.strip():
        raise ValueError("project_path is required")
    path = Path(project_path).expanduser().resolve()
    if not path.exists():
        raise ValueError(f"project_path does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"project_path is not a directory: {path}")
    return path
