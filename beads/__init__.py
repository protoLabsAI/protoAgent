"""In-process beads — the agent's issue/task tracker (Sprint B).

Replaces the file-based `br` CLI integration (operator_api/beads.py, which shelled
out against a project's `.beads/`) with a server-owned SQLite store, so beads is a
real in-process agent tool + console surface — no CLI, no filesystem-project
dependency.
"""

from beads.store import BeadsStore, VALID_STATUSES

__all__ = ["BeadsStore", "VALID_STATUSES"]
