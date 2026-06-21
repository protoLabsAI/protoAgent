"""In-process tasks — the agent's issue/task tracker (Sprint B).

Replaces the file-based `br` CLI integration (operator_api/tasks.py, which shelled
out against a project's `.tasks/`) with a server-owned SQLite store, so tasks is a
real in-process agent tool + console surface — no CLI, no filesystem-project
dependency.
"""

from tasks.store import TaskStore, VALID_STATUSES

__all__ = ["TaskStore", "VALID_STATUSES"]
