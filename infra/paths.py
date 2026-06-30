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

import contextlib
import hashlib
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

_TRUTHY = {"1", "true", "yes", "on"}


def atomic_write(path: Path | str, text: str, *, mode: int | None = None) -> None:
    """Crash-safe text write: temp file in the same directory + ``os.replace``.

    A bare ``open(path, "w")`` leaves a truncated file if the process dies
    mid-write — for the JSON/YAML registries that tolerantly load ``{}`` on a
    parse error, that silently forgets every record. The same-dir temp file
    keeps the swap on one filesystem so ``os.replace`` stays atomic.

    ``mode`` (e.g. ``0o600`` for files carrying credentials) is applied to the
    temp file *before* the swap, so the final path never exists with looser
    permissions.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def package_version() -> str:
    """This instance's app version — ``pyproject.toml`` ``[project].version`` is the
    one source of truth (the release pipeline bumps it).

    Prefer installed-package metadata; fall back to reading pyproject.toml next to
    this module (source checkout / ``COPY .`` image) or the PyInstaller bundle root;
    final fallback keeps callers valid if neither is available. Lives here (a leaf
    module) so the A2A card (``server/a2a.py``), the runtime status, and the fleet
    supervisor all report it without import cycles — the hub↔remote version
    handshake compares it.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("protoagent")
        except PackageNotFoundError:
            pass
    except ImportError:  # pragma: no cover - importlib.metadata always present on 3.11+
        pass

    import sys

    here = Path(__file__).resolve()
    if getattr(sys, "frozen", False):  # PyInstaller onefile — pyproject bundled at _MEIPASS (#894)
        candidates = [Path(getattr(sys, "_MEIPASS", here.parent))]
    else:
        # Search UPWARD for pyproject.toml: it's at the repo root (or the `COPY .`
        # image root), NOT next to this module — paths.py lives in infra/, so a
        # plain `__file__.parent` would look in infra/ and miss it (the regression
        # the root-module reorg introduced). The nearest one going up is the repo's.
        candidates = list(here.parents)
    for base in candidates:
        try:
            m = re.search(r'^version\s*=\s*"([^"]+)"', (base / "pyproject.toml").read_text(), re.MULTILINE)
            if m:
                return m.group(1)
        except OSError:
            continue
    return "0.0.0"


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


# ── data-dir version marker (migration anchor) ──────────────────────────────

DATA_VERSION: int = 1


def data_version() -> int:
    """Read ``.data-version`` from ``data_home()``, return the ``data_version`` int,
    or 0 if absent/malformed. Best-effort (never raises)."""
    import json

    try:
        f = data_home() / ".data-version"
        if f.exists():
            return json.loads(f.read_text()).get("data_version", 0)
    except Exception:  # noqa: BLE001
        pass
    return 0


def stamp_data_version(version: int | None = None) -> int:
    """Write ``.data-version`` with ``atomic_write()``. Returns the version written.
    Best-effort."""
    import json

    v = version if version is not None else DATA_VERSION
    try:
        f = data_home() / ".data-version"
        atomic_write(f, json.dumps({"data_version": v}))
        return v
    except Exception:  # noqa: BLE001
        return 0


def check_data_version() -> str | None:
    """Compare on-disk vs ``DATA_VERSION``; stamp if absent/older; return a warning
    string if on-disk is newer; else None. Best-effort."""
    try:
        on_disk = data_version()
        if on_disk == 0:
            stamp_data_version(DATA_VERSION)
            return None
        if on_disk < DATA_VERSION:
            stamp_data_version(DATA_VERSION)
            return None
        if on_disk > DATA_VERSION:
            return f"data-dir version mismatch: on-disk v{on_disk} > running v{DATA_VERSION}; downgrade is unsupported"
    except Exception:  # noqa: BLE001
        pass
    return None


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


def host_config_path() -> Path:
    """The Host-layer config file (ADR 0047) — box-shared settings (gateway/model/
    routing/telemetry defaults that all agents this machine owns inherit).

    ``scope_leaf``'d per instance like every other store, so co-located hubs stay
    isolated (#813); one-hub-per-box ≡ per-box. ``PROTOAGENT_HOST_CONFIG`` overrides
    with an explicit file path (e.g. a read-only desktop sidecar). The file is
    optional — absent ⇒ the cascade collapses to App defaults + the agent leaf.
    """
    raw = os.environ.get("PROTOAGENT_HOST_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return scope_leaf(data_home() / "host-config.yaml")


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


def user_skills_dir(*, create: bool = False) -> Path:
    """Writable root for operator-authored ``SKILL.md`` skills (``instance_root()/skills``).

    Distinct from the bundled ``config/skills`` (git-tracked, shipped examples) and
    the live ``<config_dir>/skills`` drop-in: this lives under the data home, so
    UI-managed skills survive a reboot (it's a skill seed root, re-seeded each boot)
    and stay OUT of the repo working tree. Instance-scoped, same as ``skills.db``.
    ``create=True`` mkdirs it."""
    d = instance_root() / "skills"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


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
        out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
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
        (d / f"{os.getpid()}.json").write_text(json.dumps({"pid": os.getpid(), "port": port, "identity": identity}))
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
            out.append({"pid": pid, "port": rec.get("port"), "identity": rec.get("identity") or ""})
    except Exception:  # noqa: BLE001
        return out
    return out


def colocation_warning() -> str | None:
    """A user-facing warning when another live instance shares this data root, else None."""
    others = colocated_instances()
    if not others:
        return None
    who = ", ".join(
        f"{o['identity'] or 'unknown'} (pid {o['pid']}" + (f", port {o['port']})" if o.get("port") else ")")
        for o in others
    )
    iid = instance_id()
    root = (data_home() / _safe_segment(iid)) if iid else data_home()
    return (
        f"Another running instance shares this agent's data ({root}): {who}. "
        "They can clobber each other's chat history, knowledge and stores — give each "
        "instance its own PROTOAGENT_INSTANCE id (or stop the extra one)."
    )


# ── Two-tier instance paths (box / instance) ─────────────────────────────────
# One resolution model that replaces the import-time path constants + the
# scope_leaf/_config_scope double-scoping. Resolved ONCE from the environment into
# an injectable, frozen object. Three tiers mirror the ADR-0047 cascade:
#
#   App      app_root/config        read-only bundle seed (example yaml, SOUL, presets)
#   Box      box_root               machine-shared: host-config.yaml (Host layer),
#                                    commons, heartbeats, data-version, cache
#   Instance instance_root          per-agent: config leaf, plugins, every store
#
# Resolution (two orthogonal knobs — PROTOAGENT_HOME moves only the instance tier):
#   box_root      = PROTOAGENT_BOX_ROOT  else  data_home()        # shared, never scoped
#   instance_root = PROTOAGENT_HOME                               # terminal: the dir IS the root
#                 | box_root / PROTOAGENT_INSTANCE                # named instance under the box
#                 | box_root / "default"                         # neither → "default"
#
# instance_root IS the scoped leaf — scope_leaf is never applied to it, which is
# what deletes the whole double-scope bug class.


def _app_root() -> Path:
    """Read-only bundle root: ``_MEIPASS`` when PyInstaller-frozen, else the repo
    root (two levels up from this module)."""
    import sys

    if getattr(sys, "frozen", False):  # PyInstaller onefile — bundle at _MEIPASS (#894)
        return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return Path(__file__).resolve().parents[1]


def _box_root() -> Path:
    """Machine-shared base: ``PROTOAGENT_BOX_ROOT`` override else ``data_home()``.
    Never scoped — the Host cascade layer + commons live here, inherited by every
    instance this machine owns (the default, the dev sandbox, every fleet member)."""
    raw = os.environ.get("PROTOAGENT_BOX_ROOT", "").strip()
    return Path(raw).expanduser() if raw else data_home()


@dataclass(frozen=True)
class InstancePaths:
    """Every on-disk location for one agent instance, resolved once from the
    environment (see ``instance_paths()``). Derived accessors compose the three
    roots; nothing here is computed at import time."""

    instance_id: str
    box_root: Path
    instance_root: Path
    app_root: Path

    # ── App tier (read-only bundle seed) ──
    @property
    def bundle_dir(self) -> Path:
        return self.app_root / "config"

    @property
    def config_example(self) -> Path:
        return self.bundle_dir / "langgraph-config.example.yaml"

    @property
    def soul_source(self) -> Path:
        return self.bundle_dir / "SOUL.md"

    @property
    def presets_dir(self) -> Path:
        return self.bundle_dir / "soul-presets"

    # ── Box tier (machine-shared) ──
    @property
    def host_config(self) -> Path:
        raw = os.environ.get("PROTOAGENT_HOST_CONFIG", "").strip()
        return Path(raw).expanduser() if raw else self.box_root / "host-config.yaml"

    @property
    def commons_dir(self) -> Path:
        return self.box_root / "commons"

    @property
    def commons_skills_db(self) -> Path:
        return self.commons_dir / "skills.db"

    @property
    def instances_dir(self) -> Path:
        return self.box_root / ".instances"

    @property
    def data_version_file(self) -> Path:
        return self.box_root / ".data-version"

    @property
    def cache_dir(self) -> Path:
        return self.box_root / "cache"

    # ── Instance tier (per-agent) ──
    @property
    def config_dir(self) -> Path:
        return self.instance_root / "config"

    @property
    def config_yaml(self) -> Path:
        return self.config_dir / "langgraph-config.yaml"

    @property
    def secrets_yaml(self) -> Path:
        return self.config_dir / "secrets.yaml"

    @property
    def setup_marker(self) -> Path:
        return self.config_dir / ".setup-complete"

    @property
    def theme_json(self) -> Path:
        return self.config_dir / "theme.json"

    @property
    def soul_path(self) -> Path:
        return self.config_dir / "SOUL.md"

    @property
    def plugins_dir(self) -> Path:
        raw = os.environ.get("PROTOAGENT_PLUGINS_DIR", "").strip()
        return Path(raw).expanduser() if raw else self.instance_root / "plugins"

    @property
    def plugins_lock(self) -> Path:
        raw = os.environ.get("PROTOAGENT_PLUGINS_LOCK", "").strip()
        return Path(raw).expanduser() if raw else self.instance_root / "plugins.lock"

    @property
    def workspace_dir(self) -> Path:
        """Fenced filesystem-toolset sandbox (the agent's working dir)."""
        raw = os.environ.get("PROTOAGENT_WORKSPACE", "").strip()
        return Path(raw).expanduser() if raw else self.instance_root / "workspace"

    @property
    def skills_dir(self) -> Path:
        return self.instance_root / "skills"

    @property
    def instance_uid_file(self) -> Path:
        return self.instance_root / ".instance-uid"

    # Fleet registry — HUB-instance-scoped (NOT box-shared). A member's instance_root
    # has its own (empty) workspaces/, so the supervisor's shutdown_all stays "hub-only
    # by construction" — a booting member can't read the hub registry and SIGTERM siblings.
    @property
    def workspaces_dir(self) -> Path:
        return self.instance_root / "workspaces"

    @property
    def fleet_json(self) -> Path:
        return self.workspaces_dir / "fleet.json"

    @property
    def remotes_json(self) -> Path:
        return self.workspaces_dir / "remotes.json"

    def store(self, name: str) -> Path:
        """A per-instance store path under ``instance_root`` (e.g. ``store("knowledge")``)."""
        return self.instance_root / name

    def explain(self) -> dict:
        """Flat dict of id + roots + every resolved path (powers ``config explain``)."""
        return {
            "instance_id": self.instance_id,
            "box_root": str(self.box_root),
            "instance_root": str(self.instance_root),
            "app_root": str(self.app_root),
            "paths": {
                "config_yaml": str(self.config_yaml),
                "secrets_yaml": str(self.secrets_yaml),
                "setup_marker": str(self.setup_marker),
                "theme_json": str(self.theme_json),
                "soul_path": str(self.soul_path),
                "plugins_dir": str(self.plugins_dir),
                "plugins_lock": str(self.plugins_lock),
                "workspace_dir": str(self.workspace_dir),
                "skills_dir": str(self.skills_dir),
                "workspaces_dir": str(self.workspaces_dir),
                "fleet_json": str(self.fleet_json),
                "host_config": str(self.host_config),
                "commons_dir": str(self.commons_dir),
                "instances_dir": str(self.instances_dir),
                "data_version_file": str(self.data_version_file),
                "cache_dir": str(self.cache_dir),
                "bundle_dir": str(self.bundle_dir),
            },
        }


def _resolve_instance_paths() -> InstancePaths:
    box = _box_root()
    home = os.environ.get("PROTOAGENT_HOME", "").strip()
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if home:
        root = Path(home).expanduser()
        iid = _safe_segment(inst) if inst else _safe_segment(root.name)
    elif inst:
        iid = _safe_segment(inst)
        root = box / iid
    else:
        iid = "default"
        root = box / "default"
    return InstancePaths(instance_id=iid, box_root=box, instance_root=root, app_root=_app_root())


_CURRENT_PATHS: InstancePaths | None = None


def instance_paths() -> InstancePaths:
    """The resolved paths for THIS process — resolved once from the environment on
    first call, then cached. Identity comes from env only (``PROTOAGENT_HOME`` /
    ``PROTOAGENT_INSTANCE`` / ``PROTOAGENT_BOX_ROOT``), never from config-file
    content, so a correctly-scoped config is read on the first try (no
    chicken-and-egg, no ``_seed_instance_env`` re-scope window)."""
    global _CURRENT_PATHS
    if _CURRENT_PATHS is None:
        _CURRENT_PATHS = _resolve_instance_paths()
    return _CURRENT_PATHS


def reset_instance_paths() -> None:
    """Clear the cached singleton so the next ``instance_paths()`` re-resolves from
    the (possibly monkeypatched) environment. Any test that sets a ``PROTOAGENT_*``
    root var or patches ``data_home`` MUST call this — use the autouse fixture."""
    global _CURRENT_PATHS
    _CURRENT_PATHS = None
