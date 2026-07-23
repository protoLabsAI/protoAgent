"""Provision a managed CPython runtime on demand (ADR 0094).

Downloads a pinned python-build-standalone ``install_only`` build, verifies it
against an in-repo SHA256 table (no trust-on-first-use), extracts it to the
box-shared ``runtime/python/current`` dir that ``infra.python_runtime`` points
the ``execute_code`` child spawn at, then pip-installs the document baseline
(``apps/desktop/sidecar/requirements-docs.txt`` — ADR 0092's single source of
truth) into the runtime's OWN site-packages. That last step is load-bearing:
PyInstaller packs pure-Python libs into the PYZ inside the frozen binary, so an
external interpreter can never import the bundled copies — and the child has
always run env-scrubbed with no ``PYTHONPATH``, so its library surface is the
interpreter's site-packages in both worlds (source runs work because of the
venv). Installing the baseline here reproduces that semantic exactly.

Verifying against a *checked-in* hash — not a digest served alongside the
archive — is what makes this a real integrity gate: a compromised mirror can't
swap the binary without also matching a digest we already committed. Bump
``PYTHON_VERSION``/``PBS_RELEASE`` and refresh ``_SHA256`` together (both from
https://github.com/astral-sh/python-build-standalone/releases — the release's
``SHA256SUMS``, cross-checkable against the GitHub API's per-asset digests).
The wheels the baseline step fetches ride normal pip/PyPI trust, the same trust
any server/Docker install already extends.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Callable

from infra.python_runtime import (
    _python_exe_in,
    managed_python_exe,
    managed_python_install_dir,
    managed_python_root,
)

log = logging.getLogger("protoagent.runtime.python")

# CPython 3.12 — the frozen sidecar's own interpreter line, minimizing behavior
# drift against source runs. Keep PYTHON_VERSION / PBS_RELEASE / _SHA256 in sync.
PYTHON_VERSION = "3.12.13"
PBS_RELEASE = "20260718"

# python-build-standalone target triple per (platform, arch) — the same host set the
# managed Node pins (win-arm64 et al. can be added when a host shows up needing one).
_TRIPLE: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "aarch64-apple-darwin",
    ("darwin", "x64"): "x86_64-apple-darwin",
    ("linux", "arm64"): "aarch64-unknown-linux-gnu",
    ("linux", "x64"): "x86_64-unknown-linux-gnu",
    ("win", "x64"): "x86_64-pc-windows-msvc",
}

# Pinned SHA256 of each supported ``install_only`` archive (release 20260718's
# SHA256SUMS, cross-checked against the GitHub API asset digests).
_SHA256: dict[tuple[str, str], str] = {
    ("darwin", "arm64"): "62aeee6161d57303a71a138b75fd5cc6fb8c89c4b1d9c7f0a052d89fa0b6652b",
    ("darwin", "x64"): "10b47148de86f9d87ba6e96a3db606ced90a206a3454d7d6d8fa68536a05d81f",
    ("linux", "arm64"): "53fb0a17442cd03bcfbc21df8d82e739b4901b87844934e36e54ab5122a55dfa",
    ("linux", "x64"): "7eea0959fa425c8aff3ea0a1352ee7d01d794b51439ed8f5fcfa017dbc0ec661",
    ("win", "x64"): "56c9dd9681c4810cb8bfdec277ee2606d8ab17e678e5bc2bd138eb8098e330b6",
}

# Version of the installed interpreter, so status doesn't have to execute it.
_VERSION_MARKER = ".protoagent-python-version"
# SHA256 of the requirements-docs.txt content the baseline was installed from —
# lets status say "baseline present but stale" after a pin bump, and lets install
# repair just the deps without re-downloading a matching interpreter.
_BASELINE_MARKER = ".protoagent-python-baseline"

ProgressCb = Callable[[int, int], None]
PhaseCb = Callable[[str], None]


class PythonRuntimeError(RuntimeError):
    """Base for managed-Python provisioning failures — the CLI/endpoint speaks the text."""


class UnsupportedPlatform(PythonRuntimeError):
    """No pinned CPython build for this host's platform/architecture."""


class PythonInstallError(PythonRuntimeError):
    """Download, integrity-check, extraction, or baseline-install failed."""


def _platform_arch() -> tuple[str, str]:
    """(platform, arch) for THIS host in the managed-runtime naming — e.g.
    ``('darwin', 'arm64')``. Raises ``UnsupportedPlatform`` when we don't pin a build."""
    import platform as _p

    if sys.platform.startswith("darwin"):
        plat = "darwin"
    elif sys.platform.startswith("linux"):
        plat = "linux"
    elif sys.platform.startswith("win") or os.name == "nt":
        plat = "win"
    else:
        raise UnsupportedPlatform(f"no managed Python build for platform {sys.platform!r}")

    machine = _p.machine().lower()
    if machine in ("arm64", "aarch64"):
        arch = "arm64"
    elif machine in ("x86_64", "amd64", "x64"):
        arch = "x64"
    else:
        raise UnsupportedPlatform(f"no managed Python build for architecture {machine!r}")

    if (plat, arch) not in _SHA256:
        raise UnsupportedPlatform(f"no managed Python build for {plat}-{arch}")
    return plat, arch


def _archive_name(plat: str, arch: str) -> str:
    return f"cpython-{PYTHON_VERSION}+{PBS_RELEASE}-{_TRIPLE[(plat, arch)]}-install_only.tar.gz"


def _dist_url(plat: str, arch: str) -> str:
    return (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        f"{PBS_RELEASE}/{_archive_name(plat, arch)}"
    )


def is_supported() -> bool:
    """Whether a managed CPython can be provisioned on this host."""
    try:
        _platform_arch()
        return True
    except UnsupportedPlatform:
        return False


def _managed_version() -> str | None:
    """The version of the managed install, from its marker file (no exec), falling back
    to the pinned version when a working interpreter exists without a marker."""
    if managed_python_exe() is None:
        return None
    marker = managed_python_install_dir() / _VERSION_MARKER
    try:
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip() or PYTHON_VERSION
    except OSError:
        pass
    return PYTHON_VERSION


def _baseline_requirements_path() -> Path | None:
    """The document-baseline requirements file, or None when this build doesn't carry it.

    Frozen: bundled by ``build_sidecar.py`` (``BUNDLED_DATA``) at
    ``_MEIPASS/sidecar/requirements-docs.txt``. Source: the repo file itself. A
    pip-installed (wheel) run doesn't carry it — irrelevant there, since unfrozen runs
    spawn ``sys.executable`` and never consult the managed runtime."""
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", "") or Path(sys.executable).parent)
        p = base / "sidecar" / "requirements-docs.txt"
        return p if p.exists() else None
    p = Path(__file__).resolve().parents[1] / "apps" / "desktop" / "sidecar" / "requirements-docs.txt"
    return p if p.exists() else None


def _baseline_hash(req: Path) -> str:
    return hashlib.sha256(req.read_bytes()).hexdigest()


def _baseline_state() -> tuple[bool, bool]:
    """(installed, current): whether a baseline was ever installed into the managed
    runtime, and whether it matches the requirements file this build carries. With no
    requirements file to compare against, an installed baseline counts as current."""
    marker = managed_python_install_dir() / _BASELINE_MARKER
    try:
        stamped = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    except OSError:
        stamped = ""
    if not stamped:
        return False, False
    req = _baseline_requirements_path()
    if req is None:
        return True, True
    try:
        return True, stamped == _baseline_hash(req)
    except OSError:
        return True, False


def python_status() -> dict:
    """Describe the managed-Python situation for status surfaces / the console.

    Keys:
      - ``needed``: whether THIS process would use it (frozen builds only — source
        runs spawn their own interpreter and never consult the managed runtime);
      - ``managed`` / ``managed_version`` / ``exe``: is a working install present,
        which version, and where its interpreter is;
      - ``baseline_installed`` / ``baseline_current``: document-library state
        inside the runtime (ADR 0092's requirements-docs.txt);
      - ``supported`` / ``target_version``: can we provision here, and what
        ``install`` would fetch.
    """
    exe = managed_python_exe()
    baseline_installed, baseline_current = _baseline_state() if exe is not None else (False, False)
    return {
        "needed": bool(getattr(sys, "frozen", False)),
        "managed": exe is not None,
        "managed_version": _managed_version(),
        "exe": str(exe) if exe is not None else None,
        "baseline_installed": baseline_installed,
        "baseline_current": baseline_current,
        "supported": is_supported(),
        "target_version": PYTHON_VERSION,
    }


def _download_archive(url: str, dest: Path, on_progress: ProgressCb | None, timeout: float) -> None:
    """Stream ``url`` to ``dest`` over HTTPS, reporting (downloaded, total) bytes.

    Isolated so tests can monkeypatch it with a local fixture instead of hitting the
    network. Raises ``PythonInstallError`` on any transport failure."""
    import urllib.error
    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "protoAgent-python-install"})
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
        raise PythonInstallError(f"download failed ({url}): {exc}") from exc


def _verify_sha256(path: Path, expected: str) -> None:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    got = h.hexdigest()
    if got != expected:
        raise PythonInstallError(
            f"integrity check failed for {path.name}: expected {expected}, got {got} "
            "(refusing to install an archive whose hash doesn't match the pinned digest)"
        )


def _guard_members(names: list[str]) -> None:
    """Reject archive members that escape the extraction dir (defence in depth — the
    archive is already hash-pinned, but never extract an absolute/``..`` path)."""
    for n in names:
        p = Path(n)
        if p.is_absolute() or ".." in p.parts:
            raise PythonInstallError(f"refusing unsafe archive member: {n!r}")


def _extract(archive: Path, dest: Path) -> Path:
    """Extract the CPython archive into ``dest`` and return the single top-level
    distribution dir it contains (python-build-standalone uses ``python/``)."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tf:
        names = tf.getnames()
        _guard_members(names)
        # ``filter='data'`` blocks absolute paths / traversal / device files while
        # keeping the in-tree relative symlinks the distribution uses (bin/python3 →
        # python3.12); tolerate older runtimes that lack it.
        try:
            tf.extractall(dest, filter="data")
        except TypeError:
            tf.extractall(dest)
    tops = {Path(n).parts[0] for n in names if n.strip()}
    if len(tops) != 1:
        raise PythonInstallError(f"unexpected archive layout: top-level entries {sorted(tops)!r}")
    return dest / tops.pop()


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


def _run_python(exe: Path, args: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run the managed interpreter with the parent's env (certs/proxies apply) —
    isolated so tests can stub the pip phases without a real interpreter."""
    return subprocess.run(  # noqa: S603 — argv built from pinned paths, no shell
        [str(exe), *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _ensure_pip(exe: Path) -> None:
    """Make sure the runtime can pip. ``install_only`` builds ship pip; the ensurepip
    fallback covers a stripped or future variant that doesn't."""
    if _run_python(exe, ["-m", "pip", "--version"], timeout=60).returncode == 0:
        return
    boot = _run_python(exe, ["-m", "ensurepip", "--upgrade"], timeout=180)
    if boot.returncode != 0:
        raise PythonInstallError(f"the managed runtime has no usable pip: {boot.stderr.strip()[-400:]}")


def _pip_install(exe: Path, spec_args: list[str], *, what: str, timeout: float = 600.0) -> None:
    """Pip-install ``spec_args`` (``-r <file>`` or bare specs) into the managed runtime's
    own site-packages, binary wheels only. Shared by the doc baseline and the per-plugin
    dep path (ADR 0094 P2). ``what`` names the operation for the error message."""
    _ensure_pip(exe)
    r = _run_python(
        exe,
        ["-m", "pip", "install", "--only-binary", ":all:", "--disable-pip-version-check", "--no-input", "-q", *spec_args],
        timeout=timeout,
    )
    if r.returncode != 0:
        raise PythonInstallError(f"{what} failed: {(r.stderr or r.stdout).strip()[-600:]}")


def _install_baseline(exe: Path) -> bool:
    """Pip-install the document baseline into the managed runtime's own site-packages
    and stamp the baseline marker. Returns False (a logged no-op) when this build
    doesn't carry the requirements file."""
    req = _baseline_requirements_path()
    if req is None:
        log.info("[python] no requirements-docs.txt in this build — skipping the doc baseline")
        return False
    _pip_install(exe, ["-r", str(req)], what="document-baseline install")
    (managed_python_install_dir() / _BASELINE_MARKER).write_text(_baseline_hash(req), encoding="utf-8")
    log.info("[python] document baseline installed into the managed runtime")
    return True


def install_requirements_into_managed_runtime(
    requirements: list[str], *, on_phase: PhaseCb | None = None, timeout: float = 600.0
) -> dict:
    """Pip-install arbitrary ``requirements`` into the managed runtime's site-packages
    (ADR 0094 P2) — the entry point behind "a plugin's declared deps run its
    execute_code skills on the desktop app". Requires the runtime already provisioned:
    installing deps needs an interpreter, and provisioning it is a separate consented
    step (Settings ▸ Tools). Returns ``python_status()``.

    Raises ``PythonInstallError`` if the runtime isn't provisioned (pointing at the
    install), and lets a real pip failure propagate."""
    exe = managed_python_exe()
    if exe is None:
        raise PythonInstallError(
            "the managed Python runtime isn't provisioned — install it first "
            "(Settings ▸ Tools or `protoagent runtime install-python`), then install plugin deps."
        )
    reqs = [r for r in (requirements or []) if r and r.strip()]
    if not reqs:
        return python_status()
    if on_phase is not None:
        on_phase("deps")
    _pip_install(exe, ["--", *reqs], what="plugin dependency install", timeout=timeout)
    log.info("[python] installed %d plugin requirement(s) into the managed runtime", len(reqs))
    return python_status()


def install_managed_python(
    *,
    force: bool = False,
    on_progress: ProgressCb | None = None,
    on_phase: PhaseCb | None = None,
    timeout: float = 600.0,
) -> dict:
    """Download + verify + extract the pinned CPython into the managed dir, then
    install the document baseline into it; return ``python_status()``.

    Idempotent: a matching interpreter short-circuits the download, and a matching
    baseline short-circuits the pip phase — so a stale baseline (pin bump) repairs
    with deps-only work. On any failure raises a ``PythonRuntimeError`` subclass and
    leaves any prior working install untouched."""
    plat, arch = _platform_arch()  # raises UnsupportedPlatform

    def _phase(name: str) -> None:
        if on_phase is not None:
            on_phase(name)

    if not force and managed_python_exe() is not None and _managed_version() == PYTHON_VERSION:
        _installed, current = _baseline_state()
        if not current:
            _phase("deps")
            _install_baseline(managed_python_exe())  # type: ignore[arg-type]
        return python_status()

    expected = _SHA256[(plat, arch)]
    url = _dist_url(plat, arch)
    root = managed_python_root()
    root.mkdir(parents=True, exist_ok=True)

    _phase("download")
    with tempfile.TemporaryDirectory(prefix=".python-dl-", dir=str(root)) as tmp:
        tmpdir = Path(tmp)
        archive = tmpdir / _archive_name(plat, arch)
        _download_archive(url, archive, on_progress, timeout)
        _verify_sha256(archive, expected)
        new_dist = _extract(archive, tmpdir / "x")
        # Stamp the version before the swap so status never sees a marker-less install.
        (new_dist / _VERSION_MARKER).write_text(PYTHON_VERSION, encoding="utf-8")
        if not _python_exe_in(new_dist).exists():
            raise PythonInstallError("extracted CPython is missing its interpreter executable")
        _swap_into_place(new_dist, managed_python_install_dir())

    _phase("deps")
    _install_baseline(managed_python_exe())  # type: ignore[arg-type]
    return python_status()
