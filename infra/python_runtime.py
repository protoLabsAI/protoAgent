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
