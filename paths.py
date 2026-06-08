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

import hashlib
import os
import re
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}


def _auto_scope_enabled() -> bool:
    """``PROTOAGENT_AUTO_SCOPE`` — derive a per-instance scope from the working dir when
    no explicit ``PROTOAGENT_INSTANCE`` is set, so co-located instances never silently
    share the root (#706). Opt-in (default off) — turning it on relocates an unscoped
    deployment's data to its scoped dir, so it's a deliberate switch, not automatic."""
    return os.environ.get("PROTOAGENT_AUTO_SCOPE", "").strip().lower() in _TRUTHY


def _derived_instance() -> str:
    """A stable per-working-directory scope id: ``<dirname>-<6hex of abs path>``.

    Stable across restarts (same cwd → same id), unique per directory. Instances run
    from the SAME dir (e.g. many ports) still collide — set ``PROTOAGENT_INSTANCE`` for
    those; the boot collision check warns when it happens.
    """
    cwd = str(Path.cwd().resolve())
    digest = hashlib.sha1(cwd.encode()).hexdigest()[:6]
    return _safe_segment(f"{Path(cwd).name}-{digest}")


def instance_id() -> str:
    """The active instance id, or "" for single-instance (legacy) mode.

    Explicit ``PROTOAGENT_INSTANCE`` always wins; else a working-dir-derived id when
    ``PROTOAGENT_AUTO_SCOPE`` is on; else "" (legacy shared root)."""
    explicit = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if explicit:
        return explicit
    if _auto_scope_enabled():
        return _derived_instance()
    return ""


def _safe_segment(seg: str) -> str:
    """Sanitize an id to a single safe path segment (defence against traversal)."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", seg) or "instance"


def data_home() -> Path:
    """The base data dir (``/sandbox`` in a container, else ``~/.protoagent``), unscoped."""
    sandbox = Path("/sandbox")
    return sandbox if sandbox.is_dir() else Path.home() / ".protoagent"


def unscoped_warning() -> str | None:
    """A boot warning when running UNSCOPED while the data home already holds state — an
    unscoped instance shares the loose root and can clobber a co-located sibling (#706).
    None when scoped, when auto-scope is on, or when the home is empty. Best-effort."""
    if instance_id():
        return None
    try:
        home = data_home()
        if not home.is_dir() or not any(home.iterdir()):
            return None
    except Exception:  # noqa: BLE001
        return None
    return (
        f"running UNSCOPED — stores share {home} and can clobber a co-located instance (#706). "
        "Set PROTOAGENT_AUTO_SCOPE=1 (scope per working dir) or PROTOAGENT_INSTANCE=<id> to isolate."
    )


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


def workspace_dir(*, create: bool = False) -> Path:
    """The agent's default fenced workspace — where the on-by-default filesystem
    toolset can read/write/edit (the fence the agent lives inside).

    Resolution: ``PROTOAGENT_WORKSPACE`` env wins (point it at a friendlier dir,
    e.g. from the desktop); else ``/sandbox/workspace`` in a container, falling
    back to ``~/.protoagent/workspace`` for local dev — instance-scoped either
    way. ``create=True`` mkdirs it (writes need the dir to exist)."""
    raw = os.environ.get("PROTOAGENT_WORKSPACE")
    if raw:
        base = Path(raw).expanduser()
    else:
        sandbox = Path("/sandbox/workspace")
        base = sandbox if sandbox.parent.is_dir() else Path.home() / ".protoagent" / "workspace"
        base = scope_leaf(base)
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base.resolve()
