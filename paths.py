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


# ── co-location detection (#706) ──────────────────────────────────────────────
# `unscoped_warning` above is a static boot hint — it fires for every normal
# single-instance setup, so it stays a log line. The signal worth a console
# banner is a LIVE sibling sharing this data root (two unscoped instances, or
# two scoped ones with the same id — the exact two-hubs bug): each instance
# drops a `<pid>.json` heartbeat under `<root>/.instances/` at boot, removes it
# at shutdown, and anyone can ask who else is alive in the same root.


def instance_root() -> Path:
    """THIS instance's data root: ``data_home()/<iid>`` when scoped, else the shared home."""
    home = data_home()
    iid = instance_id()
    return home / _safe_segment(iid) if iid else home


def _instances_dir() -> Path:
    """`.instances/` under THIS instance's data root (scoped or shared)."""
    return instance_root() / ".instances"


def instance_uid() -> str:
    """A stable, opaque uid for THIS data root (``<root>/.instance-uid``, created on
    first read). The console keys its per-origin client state against it: when a
    DIFFERENT backend reuses the same address (port handed from one agent to another),
    the uid mismatch tells the new tenant's window to drop the previous tenant's
    persisted chat view instead of rendering another agent's transcripts. Same root →
    same uid (same data, by design). Best-effort: "" when the root isn't writable."""
    import uuid

    try:
        f = instance_root() / ".instance-uid"
        if f.exists():
            got = f.read_text().strip()
            if got:
                return got
        f.parent.mkdir(parents=True, exist_ok=True)
        fresh = uuid.uuid4().hex[:16]
        f.write_text(fresh)
        return fresh
    except Exception:  # noqa: BLE001 — a status field, never worth failing for
        return ""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _is_protoagent_pid(pid: int) -> bool:
    """PID-reuse guard (the supervisor's #10 pattern): a heartbeat survives a crash, so a
    recycled pid could make a dead sibling look alive. Only trust a pid whose command line
    is actually a protoAgent server; if we can't inspect it, trust the pid (don't go
    quiet on odd platforms)."""
    import subprocess

    try:
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    if not out.strip():
        return False
    return "-m server" in out or ("python" in out.lower() and "server" in out)


def register_instance(port: int | None = None, identity: str = "") -> None:
    """Drop this process's heartbeat in the data root. Best-effort — never blocks boot."""
    import json

    try:
        d = _instances_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{os.getpid()}.json").write_text(json.dumps(
            {"pid": os.getpid(), "port": port, "identity": identity}))
    except Exception:  # noqa: BLE001
        pass


def unregister_instance() -> None:
    """Remove this process's heartbeat (shutdown). Best-effort."""
    try:
        (_instances_dir() / f"{os.getpid()}.json").unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def colocated_instances() -> list[dict]:
    """OTHER live protoAgent processes sharing this data root (stale entries pruned)."""
    import json

    out: list[dict] = []
    try:
        d = _instances_dir()
        if not d.is_dir():
            return out
        for f in d.glob("*.json"):
            try:
                pid = int(f.stem)
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            if not _pid_alive(pid) or not _is_protoagent_pid(pid):
                f.unlink(missing_ok=True)  # stale heartbeat (crash / pid recycled)
                continue
            try:
                rec = json.loads(f.read_text())
            except (OSError, ValueError):
                rec = {}
            out.append({"pid": pid, "port": rec.get("port"),
                        "identity": rec.get("identity") or ""})
    except Exception:  # noqa: BLE001
        return out
    return out


def colocation_warning() -> str | None:
    """A user-facing warning when another live instance shares this data root, else None."""
    others = colocated_instances()
    if not others:
        return None
    who = ", ".join(
        f"{o['identity'] or 'unknown'} (pid {o['pid']}"
        + (f", port {o['port']})" if o.get("port") else ")")
        for o in others)
    iid = instance_id()
    root = (data_home() / _safe_segment(iid)) if iid else data_home()
    return (f"Another running instance shares this agent's data ({root}): {who}. "
            "They can clobber each other's chat history, knowledge and stores — give each "
            "instance its own PROTOAGENT_INSTANCE id (or stop the extra one).")
