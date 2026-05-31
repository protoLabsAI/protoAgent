"""Per-instance data-path scoping (ADR 0004).

Multiple protoAgent instances on one shared filesystem must not clobber each
other's on-disk state. When an **instance id** is set (``PROTOAGENT_INSTANCE``
env, seeded from ``instance_id`` config at startup), every store nests its files
under that id; when unset, paths are byte-identical to the single-instance
default — so existing deployments need no migration, and containers (each with
its own ``/sandbox``) are unaffected.

``scope_leaf`` is the one knob: applied to a store's final resolved path, it
inserts the instance segment as the leaf's parent dir (a no-op when no id is
set). Apply it at the end of each resolver, *after* the writable-fallback choice,
so the segment survives a ``/sandbox`` → ``~/.protoagent`` fallback.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


def instance_id() -> str:
    """The active instance id, or "" for single-instance (legacy) mode."""
    return os.environ.get("PROTOAGENT_INSTANCE", "").strip()


def _safe_segment(seg: str) -> str:
    """Sanitize an id to a single safe path segment (defence against traversal)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", seg) or "instance"


def scope_leaf(path: str | Path) -> Path:
    """Insert the instance id as the parent dir of ``path``'s leaf when set.

    ``/sandbox/checkpoints.db`` → ``/sandbox/<id>/checkpoints.db``;
    ``~/.protoagent/knowledge/agent.db`` → ``~/.protoagent/knowledge/<id>/agent.db``.
    A no-op (returns ``path`` unchanged) when no instance id is configured.
    """
    p = Path(str(path)).expanduser()
    iid = instance_id()
    if not iid:
        return p
    return p.parent / _safe_segment(iid) / p.name
