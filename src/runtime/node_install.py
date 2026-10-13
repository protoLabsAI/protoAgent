"""Provision a managed Node.js runtime on demand (ADR 0085).

Downloads a pinned Node release from nodejs.org, verifies it against an in-repo
SHA256 table (no trust-on-first-use), and extracts it to the box-shared
``runtime/node/current`` dir that ``infra.node_runtime`` points every subprocess
launch at. This is the "provision on demand" answer to the desktop ``npx`` gap:
the installer ships lean, and a user who wants the ``npx``-based coding agents
(Claude Code, Codex) or MCP servers installs the runtime with one command
(``protoagent runtime install-node``) or, later, one console click.

Verifying against a *checked-in* hash — not the ``SHASUMS256.txt`` the same server
serves — is what makes this a real integrity gate rather than trust-on-first-use: a
compromised mirror can't swap the binary without also matching a digest we already
committed. Bump ``NODE_VERSION`` and refresh ``_SHA256`` together (both come from
https://nodejs.org/dist/<NODE_VERSION>/SHASUMS256.txt).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Callable

from infra.node_runtime import (
    _bin_dir_for,
    _node_exe_name,
    augment_path_with_managed_node,
    managed_node_bin_dir,
    managed_node_install_dir,
    managed_node_root,
    node_on_path,
)

# Node 24 LTS ("Krypton"). Keep in sync with _SHA256 below.
NODE_VERSION = "v24.18.0"

# Pinned SHA256 of each supported distribution archive (from
# https://nodejs.org/dist/v24.18.0/SHASUMS256.txt). The (platform, arch) keys use
# nodejs.org's own naming.
_SHA256: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "e1a97e14c99c803e96c7339403282ea05a499c32f8d83defe9ef5ec66f979ed1",
    ("darwin", "x64"): "dfd0dbd3e721503434df7b7205e719f61b3a3a31b2bcf9729b8b91fea240f080",
    ("linux", "arm64"): "6b4484c2190274175df9aa8f28e2d758a819cb1c1fe6ab481e2f95b463ab8508",
    ("linux", "x64"): "783130984963db7ba9cbd01089eaf2c2efb055c7c1693c943174b967b3050cb8",
    ("win", "x64"): "0ae68406b42d7725661da979b1403ec9926da205c6770827f33aac9d8f26e821",
}

# Filename holding the installed version, so status doesn't have to execute node.
_VERSION_MARKER = ".protoagent-node-version"

ProgressCb = Callable[[int, int], None]


class NodeRuntimeError(RuntimeError):
    """Base for managed-Node provisioning failures — the CLI/endpoint speaks the text."""


class UnsupportedPlatform(NodeRuntimeError):
    """No pinned Node build for this host's platform/architecture."""


class NodeInstallError(NodeRuntimeError):
    """Download, integrity-check, or extraction failed."""


def _platform_arch() -> tuple[str, str]:
    """(node_platform, node_arch) for THIS host in nodejs.org's naming — e.g.
    ``('darwin', 'arm64')``, ``('linux', 'x64')``. Raises ``UnsupportedPlatform`` when
    we don't pin a build for it."""
    import platform as _p

    if sys.platform.startswith("darwin"):
        plat = "darwin"
    elif sys.platform.startswith("linux"):
        plat = "linux"
    elif sys.platform.startswith("win") or os.name == "nt":
        plat = "win"
    else:
        raise UnsupportedPlatform(f"no managed Node build for platform {sys.platform!r}")

    machine = _p.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        raise UnsupportedPlatform(f"no managed Node build for architecture {machine!r}")

    if (plat, arch) not in _SHA256:
        raise UnsupportedPlatform(f"no managed Node build for {plat}-{arch}")
    return plat, arch


def _archive_name(plat: str, arch: str) -> str:
    ext = "zip" if plat == "win" else "tar.gz"
    return f"node-{NODE_VERSION}-{plat}-{arch}.{ext}"


def _dist_url(plat: str, arch: str) -> str:
    return f"https://nodejs.org/dist/{NODE_VERSION}/{_archive_name(plat, arch)}"


def is_supported() -> bool:
    """Whether a managed Node can be provisioned on this host."""
    try:
        _platform_arch()
        return True
    except UnsupportedPlatform:
        return False


def _node_version(node_exe: str) -> str | None:
    """``<node> --version`` (stripped, e.g. ``v24.18.0``), or None if it won't run."""
    try:
        out = subprocess.run(
            [node_exe, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    got = (out.stdout or "").strip()
    return got or None


def _managed_version() -> str | None:
    """The version of the managed install, from its marker file (no exec), falling back
    to the pinned version when a working bin dir exists without a marker."""
    if managed_node_bin_dir() is None:
        return None
    marker = managed_node_install_dir() / _VERSION_MARKER
    try:
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip() or NODE_VERSION
    except OSError:
        pass
    return NODE_VERSION


def node_status() -> dict:
    """Describe the Node situation for status surfaces / the console.

    Keys:
      - ``source``: ``'system'`` (a Node on PATH we didn't provision), ``'managed'``,
        or ``None`` (no usable Node) — which one would actually launch ``npx`` today;
      - ``version``: the effective Node version string, or None;
      - ``bin_dir``: the effective bin dir, or None;
      - ``managed`` / ``managed_version``: whether we've provisioned one, and which;
      - ``system``: whether a non-managed Node is on PATH;
      - ``supported``: can we provision here?;
      - ``target_version``: the version ``install`` would fetch.
    """
    managed_bin = managed_node_bin_dir()
    managed_version = _managed_version()

    # A "system" Node means the user's OWN install. If PATH resolution lands inside the
    # managed bin dir (because augment already ran this process), that's not a user
    # install — discount it so status doesn't mislabel managed as system.
    system = node_on_path()
    if system and managed_bin is not None and Path(system).resolve().parent == managed_bin.resolve():
        system = None

    if system:
        source, bin_dir, version = "system", str(Path(system).parent), _node_version(system)
    elif managed_bin is not None:
        source, bin_dir, version = "managed", str(managed_bin), managed_version
    else:
        source, bin_dir, version = None, None, None

    return {
        "source": source,
        "version": version,
        "bin_dir": bin_dir,
        "managed": managed_bin is not None,
        "managed_version": managed_version,
        "system": bool(system),
        "supported": is_supported(),
        "target_version": NODE_VERSION,
    }


def _download_archive(url: str, dest: Path, on_progress: ProgressCb | None, timeout: float) -> None:
    """Stream ``url`` to ``dest`` over HTTPS, reporting (downloaded, total) bytes.

    Isolated so tests can monkeypatch it with a local fixture instead of hitting the
    network. Raises ``NodeInstallError`` on any transport failure."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "protoAgent-node-install"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — pinned https host
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    if on_progress is not None:
                        on_progress(done, total)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise NodeInstallError(f"download failed ({url}): {exc}") from exc


def _verify_sha256(path: Path, expected: str) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        raise NodeInstallError(
            f"integrity check failed for {path.name}: expected {expected}, got {got} "
            "(refusing to install an archive whose hash doesn't match the pinned digest)"
        )


def _extract(archive: Path, dest: Path) -> Path:
    """Extract the Node archive into ``dest`` and return the single top-level
    distribution dir it contains (``node-<version>-<plat>-<arch>/``)."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            _guard_members(names)
            zf.extractall(dest)
    else:
        with tarfile.open(archive, "r:gz") as tf:
            names = tf.getnames()
            _guard_members(names)
            # ``filter='data'`` (3.12+, backported to recent 3.11.x) blocks absolute
            # paths / traversal / device files; tolerate older runtimes that lack it.
            try:
                tf.extractall(dest, filter="data")
            except TypeError:
                tf.extractall(dest)
    tops = {Path(n).parts[0] for n in names if n.strip()}
    dist = [t for t in tops if t.startswith("node-")]
    if len(dist) != 1:
        raise NodeInstallError(f"unexpected archive layout: top-level entries {sorted(tops)!r}")
    return dest / dist[0]


def _guard_members(names: list[str]) -> None:
    """Reject archive members that escape the extraction dir (defence in depth — the
    archive is already hash-pinned, but never extract an absolute/``..`` path)."""
    for n in names:
        p = Path(n)
        if p.is_absolute() or ".." in p.parts:
            raise NodeInstallError(f"refusing unsafe archive member: {n!r}")


def _swap_into_place(new_dist: Path, final: Path) -> None:
    """Move ``new_dist`` to ``final`` (``…/current``), replacing any prior install, and
    keep a working install intact on failure: the old dir is only removed once the new
    one is in place."""
    final.parent.mkdir(parents=True, exist_ok=True)
    backup = final.parent / f".old-{os.getpid()}"
    had_old = final.exists()
    if had_old:
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        os.replace(final, backup)
    try:
        os.replace(new_dist, final)
    except OSError:
        if had_old:  # restore the previous working install
            os.replace(backup, final)
        raise
    if had_old:
        shutil.rmtree(backup, ignore_errors=True)


def install_managed_node(
    *, force: bool = False, on_progress: ProgressCb | None = None, timeout: float = 300.0
) -> dict:
    """Download + verify + extract the pinned Node into the managed dir; return
    ``node_status()``.

    Idempotent: a matching managed install short-circuits unless ``force=True``. On any
    failure raises a ``NodeRuntimeError`` subclass and leaves any prior working install
    untouched. On success, re-runs the PATH augmentation so a *live* server picks up the
    new runtime without a restart."""
    plat, arch = _platform_arch()  # raises UnsupportedPlatform

    if not force and managed_node_bin_dir() is not None and _managed_version() == NODE_VERSION:
        return node_status()

    expected = _SHA256[(plat, arch)]
    url = _dist_url(plat, arch)
    root = managed_node_root()
    root.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=".node-dl-", dir=str(root)) as tmp:
        tmpdir = Path(tmp)
        archive = tmpdir / _archive_name(plat, arch)
        _download_archive(url, archive, on_progress, timeout)
        _verify_sha256(archive, expected)
        new_dist = _extract(archive, tmpdir / "x")
        # Stamp the version before the swap so status never sees a marker-less install.
        (new_dist / _VERSION_MARKER).write_text(NODE_VERSION, encoding="utf-8")
        bin_dir = _bin_dir_for(new_dist)
        if not (bin_dir / _node_exe_name()).exists():
            raise NodeInstallError(f"extracted Node is missing its {_node_exe_name()} executable")
        _swap_into_place(new_dist, managed_node_install_dir())

    augment_path_with_managed_node()
    return node_status()
