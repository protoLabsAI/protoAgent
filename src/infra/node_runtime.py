"""Managed Node.js runtime discovery + PATH wiring (ADR 0085).

protoAgent's npm-ecosystem extensions need ``node``/``npx`` on PATH: the ACP
coding agents launched via ``npx`` (Claude Code, Codex) and the many
``npx``-based MCP servers. A desktop user who never installed Node has neither,
so those features dead-end with *"agent binary not found: 'npx'"* — the existing
PATH-discovery machinery (``discovered_node_dirs`` below — the nvm/fnm/Volta/asdf/
nodenv/Homebrew probe the ACP client used to own privately — and
``augmented_sidecar_path`` in the Tauri shell) can only *find* a Node the user
already has; it can't conjure one that isn't installed.

The "provision on demand" answer (``runtime.node_install``) downloads a pinned
Node into the box-shared data dir. This module is the light, dependency-free half
consumed everywhere else: where that runtime lives, and the single seam that makes
it visible to every subprocess the server spawns —
``augment_path_with_managed_node()``, called once at boot (``server.agent_init``)
and again right after an install so a live server hot-adopts it without a restart.

Kept in ``infra`` (a leaf every layer may import) with no heavy deps, so the ACP
client, the MCP launcher, the server bootstrap, the CLI, the plugin deps gate and
``sdk.node_executable`` all share ONE notion of the managed runtime — they can never
disagree about where it is.

Version-aware since the 2026-10-15 amendment to ADR 0085: ``resolve_node(min_version)``
picks an ABSOLUTE ``node`` that satisfies a version floor (PATH → discovered version
managers → managed), and the boot-time PATH seam prepends the managed runtime when the
user's own Node is older than ``CORE_NODE_FLOOR``.
"""

from __future__ import annotations

import glob
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

log = logging.getLogger("protoagent.infra.node")

#: The oldest Node major core treats as usable for the processes it spawns (npx-based ACP
#: agents, MCP servers, plugin bundles). A PATH Node below this is shadowed by the managed
#: runtime, when one is installed (``augment_path_with_managed_node``).
CORE_NODE_FLOOR = "20"

#: Opt-out for that shadowing: ``PROTOAGENT_NODE_SHADOW=0`` keeps an old PATH Node first
#: (the managed runtime is not prepended); the below-floor condition is still logged once.
NODE_SHADOW_ENV = "PROTOAGENT_NODE_SHADOW"

# The managed install is extracted to a stable ``current/`` dir (a real directory,
# not a symlink — Windows symlink creation needs elevation) so consumers resolve
# the bin dir without knowing the pinned version.
_CURRENT = "current"


def managed_node_root() -> Path:
    """The box-shared dir holding provisioned Node runtimes (``box_root/runtime/node``).

    Box-tier, not instance-tier: one machine provisions Node once and every instance
    (the default, the dev sandbox, every fleet member) shares it. A per-instance copy
    would waste ~150 MB each and re-download on every ``dev-reset``."""
    from infra.paths import instance_paths

    return instance_paths().box_root / "runtime" / "node"


def managed_node_install_dir() -> Path:
    """The stable path a provisioned Node is extracted into (``…/runtime/node/current``)."""
    return managed_node_root() / _CURRENT


def _bin_dir_for(install_dir: Path) -> Path:
    """The dir holding the ``node`` executable inside an extracted distribution:
    ``<root>/bin`` on POSIX tarballs, the distribution root itself on the Windows zip
    (which puts ``node.exe``/``npx.cmd`` at the top level)."""
    return install_dir if os.name == "nt" else install_dir / "bin"


def _node_exe_name() -> str:
    return "node.exe" if os.name == "nt" else "node"


def managed_node_bin_dir() -> Path | None:
    """The bin dir of a *working* managed Node install, or None.

    "Working" means the expected ``node`` executable actually exists — a
    half-extracted, wiped, or never-installed runtime returns None so callers fall
    back cleanly instead of adding a dead dir to PATH."""
    bin_dir = _bin_dir_for(managed_node_install_dir())
    return bin_dir if (bin_dir / _node_exe_name()).exists() else None


def node_on_path(path: str | None = None) -> str | None:
    """Absolute path to a ``node`` already resolvable on ``path`` (default: the process
    PATH), or None. This is the "user already has Node" check — when it's truthy and new
    enough (``CORE_NODE_FLOOR``) we never touch PATH; a user's own install wins.

    Called positionally for the default (``shutil.which`` already falls back to the
    process PATH) so the common path stays a plain one-arg call."""
    return shutil.which("node") if path is None else shutil.which("node", path=path)


# ── versions ─────────────────────────────────────────────────────────────────

_VERSION_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")
# A version FLOOR as plugins and the SDK spell it: ``20``, ``20.6``, ``20.6.1`` (an
# optional leading ``v``). Manifest specs add a ``>=`` in front (``manifest._parse_requires_runtimes``).
_FLOOR_RE = re.compile(r"^v?\d+(?:\.\d+){0,2}$")


def parse_node_version(text: str) -> tuple[int, int, int] | None:
    """``"v22.11.0"`` / ``"20"`` / ``"20.6"`` → ``(22, 11, 0)`` / ``(20, 0, 0)`` / ``(20, 6, 0)``;
    None when ``text`` doesn't start with a version number."""
    m = _VERSION_RE.match(str(text or ""))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def parse_min_version(min_version: str | None) -> tuple[int, int, int] | None:
    """Validate a version floor (``"20"``, ``"20.6"``, ``"20.6.1"``; a leading ``>=`` or ``v``
    is tolerated). None in → None out (no floor). Raises ``ValueError`` on anything else."""
    if min_version is None:
        return None
    text = str(min_version).strip()
    if text.startswith(">="):
        text = text[2:].strip()
    if not _FLOOR_RE.match(text):
        raise ValueError(f"invalid Node version floor {min_version!r} (expected e.g. '20' or '20.6.1')")
    return parse_node_version(text)


def format_node_version(version: tuple[int, ...]) -> str:
    return "v" + ".".join(str(n) for n in version)


_VERSION_CACHE: dict[tuple[str, int, int], tuple[int, int, int] | None] = {}
_VERSION_LOCK = threading.Lock()


def node_version(exe: str | os.PathLike) -> tuple[int, int, int] | None:
    """The version of the ``node`` at ``exe`` (it runs ``exe --version``, 10 s timeout),
    or None when it isn't there, won't run, or prints no version.

    Cached by ``(path, mtime, size)``, so a resolver called on every plugin load
    doesn't fork a process per candidate per call, and an upgraded binary at the same path
    is re-read."""
    path = os.path.abspath(os.fspath(exe))
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    with _VERSION_LOCK:
        if key in _VERSION_CACHE:
            return _VERSION_CACHE[key]
    try:
        # No console window flashes up per probe on Windows (a GUI-launched server has none).
        extra = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        out = subprocess.run(  # noqa: S603 — an absolute node path we resolved, fixed args
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False, **extra
        )
        version = parse_node_version(out.stdout or "") if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        version = None
    with _VERSION_LOCK:
        _VERSION_CACHE[key] = version
    return version


def _reset_version_cache() -> None:
    """Test hook — forget every probed version."""
    with _VERSION_LOCK:
        _VERSION_CACHE.clear()


# ── discovery (version managers + standard locations) ────────────────────────


def _version_sort_key(bin_dir: str) -> tuple[int, ...]:
    """Sort key for a version-manager node `bin` dir, newest-first. The version is the
    dir component holding the semver (``…/node/v22.22.0/bin`` → ``v22.22.0``; fnm's
    ``…/<ver>/installation/bin`` → the grandparent). Non-versioned dirs sort last."""
    p = Path(bin_dir)
    for name in (p.parent.name, p.parent.parent.name):
        nums = re.findall(r"\d+", name)
        if nums:
            return tuple(int(n) for n in nums[:3])
    return (0,)


@lru_cache(maxsize=1)
def discovered_node_dirs() -> tuple[str, ...]:
    """Existing node `bin` dirs from the common version managers + standard locations,
    most-preferred first (newest version). Only dirs that actually contain a ``node``
    executable are returned.

    A server started as a service (systemd, launchd, a bare ``nohup``) gets a MINIMAL
    PATH: nvm / fnm / Volta / asdf / nodenv all put their bin dir on PATH from the *shell
    rc*, which a service never sources. This probe finds those installs anyway. Cached —
    the filesystem probe runs once per process. Best-effort: any error yields fewer dirs."""
    home = Path.home()
    cands: list[str] = []

    def _versioned(root: str | os.PathLike, pattern: str) -> None:
        try:
            hits = glob.glob(os.path.join(str(root), pattern))
        except OSError:
            return
        cands.extend(sorted(hits, key=_version_sort_key, reverse=True))

    # nvm: ~/.nvm/versions/node/<ver>/bin   (NVM_DIR overrides the location)
    _versioned(os.environ.get("NVM_DIR") or home / ".nvm", "versions/node/*/bin")
    # fnm: <root>/node-versions/<ver>/installation/bin
    for fnm_root in (os.environ.get("FNM_DIR"), home / ".local/share/fnm", home / ".fnm"):
        if fnm_root:
            _versioned(fnm_root, "node-versions/*/installation/bin")
    # Single shim/bin dirs: volta, asdf, nodenv, homebrew, common system.
    cands.append(str(Path(os.environ.get("VOLTA_HOME") or home / ".volta") / "bin"))
    cands.append(str(Path(os.environ.get("ASDF_DATA_DIR") or home / ".asdf") / "shims"))
    cands.append(str(home / ".nodenv" / "shims"))
    cands.extend(["/opt/homebrew/bin", "/usr/local/bin"])

    out: list[str] = []
    seen: set[str] = set()
    for d in cands:
        if d in seen:
            continue
        seen.add(d)
        if os.path.exists(os.path.join(d, "node")) or os.path.exists(os.path.join(d, "node.exe")):
            out.append(d)
    return tuple(out)


def _node_in(bin_dir: str | os.PathLike) -> str | None:
    for name in (_node_exe_name(), "node"):
        cand = os.path.join(os.fspath(bin_dir), name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


# ── resolution ───────────────────────────────────────────────────────────────

NodeSource = Literal["path", "discovered", "managed"]


@dataclass(frozen=True)
class NodeResolution:
    """One resolved ``node``: its ABSOLUTE path, its version, and where it came from —
    ``"path"`` (the process PATH), ``"discovered"`` (a version-manager / Homebrew install
    off PATH), or ``"managed"`` (the ADR 0085 runtime)."""

    path: Path
    version: tuple[int, int, int]
    source: NodeSource

    @property
    def version_str(self) -> str:
        return format_node_version(self.version)


def _satisfies(version: tuple[int, int, int] | None, floor: tuple[int, int, int] | None) -> bool:
    return version is not None and (floor is None or version >= floor)


def _is_managed(exe: str, managed_bin: Path | None) -> bool:
    if managed_bin is None:
        return False
    try:
        return Path(exe).resolve().parent == managed_bin.resolve()
    except OSError:
        return False


def resolve_node(min_version: str | None = None) -> NodeResolution | None:
    """The best ``node`` for a caller needing at least ``min_version`` (``"20"``,
    ``"20.6.1"``; None = any version), or None when nothing satisfies it.

    Order: the process PATH (the user's choice wins when it's new enough) → the
    version-manager / Homebrew dirs ``discovered_node_dirs`` finds off-PATH (newest
    first) → the managed runtime. Every candidate is version-checked by running it
    (cached); the returned path is always ABSOLUTE, so a caller spawns exactly the
    binary that was checked rather than whatever PATH resolves at spawn time. Raises
    ``ValueError`` for a malformed ``min_version``."""
    floor = parse_min_version(min_version)
    managed_bin = managed_node_bin_dir()
    seen: set[str] = set()

    def _try(exe: str | None, source: NodeSource) -> NodeResolution | None:
        if not exe:
            return None
        exe = os.path.abspath(exe)
        if exe in seen:
            return None
        seen.add(exe)
        version = node_version(exe)
        if not _satisfies(version, floor):
            return None
        # PATH can point INTO the managed dir once augment ran — call that what it is.
        if source != "managed" and _is_managed(exe, managed_bin):
            source = "managed"
        return NodeResolution(Path(exe), version, source)  # type: ignore[arg-type]

    hit = _try(node_on_path(), "path")
    if hit:
        return hit
    for d in discovered_node_dirs():
        hit = _try(_node_in(d), "discovered")
        if hit:
            return hit
    if managed_bin is not None:
        return _try(_node_in(managed_bin), "managed")
    return None


# ── PATH wiring (the consumption seam) ───────────────────────────────────────

_SHADOW_LOGGED = False


def augment_path_with_managed_node() -> str | None:
    """Make the managed Node visible to every subprocess this process spawns, by
    editing ``os.environ['PATH']`` — only when a managed install actually exists, and
    only when the user's own Node doesn't already do the job:

    * no ``node`` on PATH → APPEND the managed bin dir (fill the gap; an explicitly
      configured PATH keeps priority for everything else it provides);
    * a PATH ``node`` OLDER than ``CORE_NODE_FLOOR`` → PREPEND it, so spawned processes
      get a Node that works instead of the stale one (logged once). ``PROTOAGENT_NODE_SHADOW=0``
      opts out: PATH is left alone and only the log line remains;
    * a PATH ``node`` that meets the floor (or whose version can't be read) → untouched:
      the user's own install wins.

    Idempotent: once the managed dir wins resolution a second call is a no-op. Returns the
    dir added, or None when nothing changed. Mutating the *process* env once (rather than
    each launch site) means the ACP client, the MCP launcher, delegates, and ``gh``/git
    all inherit the runtime uniformly — one seam, not one patch per spawn call."""
    global _SHADOW_LOGGED
    bin_dir = managed_node_bin_dir()
    if bin_dir is None:
        return None
    added = str(bin_dir)
    path = os.environ.get("PATH") or os.defpath
    parts = path.split(os.pathsep) if path else []
    current = node_on_path()
    if current is None:
        if added in parts:
            return None
        os.environ["PATH"] = os.pathsep.join([path, added]) if path else added
        return added
    if _is_managed(current, bin_dir):
        return None  # the managed runtime already wins
    version = node_version(current)
    floor = parse_min_version(CORE_NODE_FLOOR)
    if version is None or _satisfies(version, floor):
        return None  # the user's own Node is new enough (or unreadable — don't second-guess it)
    if os.environ.get(NODE_SHADOW_ENV, "").strip() == "0":
        if not _SHADOW_LOGGED:
            _SHADOW_LOGGED = True
            log.info(
                "[node] PATH node %s is %s, older than core's floor (>=%s); NOT shadowing it "
                "with the managed runtime (%s) because %s=0",
                current,
                format_node_version(version),
                CORE_NODE_FLOOR,
                added,
                NODE_SHADOW_ENV,
            )
        return None
    os.environ["PATH"] = os.pathsep.join([added, *(p for p in parts if p != added)])
    if not _SHADOW_LOGGED:
        _SHADOW_LOGGED = True
        log.info(
            "[node] PATH node %s is %s, older than core's floor (>=%s) — the managed runtime "
            "(%s) now comes first for spawned processes",
            current,
            format_node_version(version),
            CORE_NODE_FLOOR,
            added,
        )
    return added
