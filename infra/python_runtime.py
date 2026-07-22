"""Managed Python runtime discovery (ADR 0094).

The packaged desktop app is a PyInstaller sidecar, so ``sys.executable`` is the
frozen server binary — there is no interpreter to spawn, which is why
``execute_code`` (and with it every compute-through-code capability, e.g.
cowork's document skills) was structurally dead on desktop (#2137). The
"provision on demand" answer (``runtime.python_install``) downloads a pinned
CPython into the box-shared data dir; this module is the light, dependency-free
half consumed everywhere else: where that runtime lives and whether a working
interpreter is actually there.

Deliberately narrower than ``infra.node_runtime``: there is NO PATH
augmentation and NO "a system Python wins" discovery. The managed interpreter
exists for exactly one consumer — the ``execute_code`` child spawn — and a
discovered user Python of arbitrary version with arbitrary site-packages would
reproduce #2137's failure class (silently missing capability) with worse
debuggability. Source runs never come here at all; they spawn
``sys.executable`` as they always have.

Kept in ``infra`` (a leaf every layer may import) with no heavy deps, so the
plugin engine, the operator routes, and the CLI share ONE notion of the managed
runtime — they can never disagree about where it is.
"""

from __future__ import annotations

import os
from pathlib import Path

# The managed install is extracted to a stable ``current/`` dir (a real directory,
# not a symlink — Windows symlink creation needs elevation) so consumers resolve
# the interpreter without knowing the pinned version.
_CURRENT = "current"


def managed_python_root() -> Path:
    """The box-shared dir holding provisioned Python runtimes (``box_root/runtime/python``).

    Box-tier, not instance-tier, like the managed Node (ADR 0085): one machine
    provisions once and every instance (the default, the dev sandbox, every fleet
    member) shares it."""
    from infra.paths import instance_paths

    return instance_paths().box_root / "runtime" / "python"


def managed_python_install_dir() -> Path:
    """The stable path a provisioned CPython is extracted into (``…/runtime/python/current``)."""
    return managed_python_root() / _CURRENT


def _python_exe_in(install_dir: Path) -> Path:
    """The interpreter inside an extracted python-build-standalone ``install_only``
    distribution: ``<root>/bin/python3`` on POSIX, ``<root>/python.exe`` on Windows
    (the Windows layout puts the exe at the distribution root)."""
    return install_dir / "python.exe" if os.name == "nt" else install_dir / "bin" / "python3"


def managed_python_exe() -> Path | None:
    """The interpreter of a *working* managed install, or None.

    "Working" means the expected executable actually exists — a half-extracted,
    wiped, or never-installed runtime returns None so the caller can speak the
    install path instead of attempting a broken spawn."""
    exe = _python_exe_in(managed_python_install_dir())
    return exe if exe.exists() else None


def _normalize_dist(name: str) -> str:
    """PEP 503 normalized distribution name (``Python_DocX`` → ``python-docx``) so a
    requirement spec and an installed ``.dist-info`` match regardless of case or the
    ``-``/``_``/``.`` the two happen to use."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def managed_runtime_distributions() -> set[str]:
    """The normalized names of distributions installed in the managed runtime's own
    site-packages (ADR 0094 P2) — read from the ``.dist-info``/``.egg-info`` dir names,
    NOT by spawning the interpreter, so it's cheap enough for the per-plugin
    ``deps_missing`` check on every ``/api/plugins/installed`` call.

    Empty when no runtime is provisioned. This is how the frozen desktop app knows a
    plugin's declared deps are already in the child runtime (installed there by
    ``runtime.python_install``) even though they're NOT importable in the host process
    — the two have separate site-packages."""
    install_dir = managed_python_install_dir()
    if not install_dir.exists():
        return set()
    dists: set[str] = set()
    # install_only layout: POSIX ``lib/pythonX.Y/site-packages``, Windows ``Lib/site-packages``.
    for site in (*install_dir.glob("lib/python3.*/site-packages"), install_dir / "Lib" / "site-packages"):
        if not site.is_dir():
            continue
        for meta in (*site.glob("*.dist-info"), *site.glob("*.egg-info")):
            # ``<name>-<version>.dist-info`` — wheel-normalized names replace project-name
            # dashes with ``_``, so the only ``-`` is the name/version separator.
            stem = meta.name.rsplit(".", 1)[0]
            dists.add(_normalize_dist(stem.rsplit("-", 1)[0]))
    return dists
