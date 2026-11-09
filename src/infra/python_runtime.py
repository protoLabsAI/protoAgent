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

Since the 2026-11-08 amendment to ADR 0085 this module ALSO owns per-plugin
virtual environments: a plugin that declares ``requires_runtimes: {python:
{version, requirements}}`` gets its own ``<plugin_store>/.venv`` provisioned with
``uv`` on the operator's Install click (see :func:`provision_plugin_venv`). That
is separate from the box-shared managed runtime above — each plugin's heavy deps
stay in its own venv and never load into the host process — but it lives here so
the manifest schema and the one provisioner can't drift apart.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("protoagent.infra.python")

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


def normalize_dist(name: str) -> str:
    """PEP 503 normalized distribution name (``Python_DocX`` → ``python-docx``) so a
    requirement spec and an installed ``.dist-info`` match regardless of case or the
    ``-``/``_``/``.`` the two happen to use. Public: the plugin installer's dep
    resolution normalizes through this too, so the two can never disagree."""
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
            dists.add(normalize_dist(stem.rsplit("-", 1)[0]))
    return dists


def managed_runtime_distribution_versions() -> dict[str, str]:
    """``{normalized name: version}`` for the managed runtime's site-packages.

    The version-carrying sibling of :func:`managed_runtime_distributions`, for callers
    that must honour a requirement's specifier rather than only its name — a plugin's
    ``requires_pip`` pins into this same runtime, so "a dist by that name exists" is not
    the same question as "it satisfies ``>=8``"."""
    versions: dict[str, str] = {}
    install_dir = managed_python_install_dir()
    if not install_dir.exists():
        return versions
    for site in (*install_dir.glob("lib/python3.*/site-packages"), install_dir / "Lib" / "site-packages"):
        if not site.is_dir():
            continue
        for meta in (*site.glob("*.dist-info"), *site.glob("*.egg-info")):
            stem = meta.name.rsplit(".", 1)[0]
            name, _, version = stem.rpartition("-")
            if not name:  # no version segment — nothing to compare against
                continue
            versions[normalize_dist(name)] = version
    return versions


def pytest_interpreter() -> tuple[str | None, str | None]:
    """``(python, error)`` — the interpreter to spawn ``-m pytest`` with (ADR 0096 D3).

    Source-run → ``sys.executable``. Frozen → the ADR 0094 managed runtime, and only
    when pytest is actually installed *there*: the app bundle carries no pytest, and
    the managed runtime's baseline is the ADR 0092 **document** stack (docx/xlsx/pptx/
    pdf), so a provisioned runtime is not on its own enough. Never falls back to a
    discovered system Python — see this module's docstring for why that discovery is
    deliberately absent.

    Both refusals name the remedy instead of letting the child fail with a bare
    ``No module named pytest``: the caller is usually an agent deciding what to do
    next, and "install X in Settings" is actionable where a traceback is not.

    Shared by every in-tree pytest spawner (plugin-devkit's ``test_plugin``, the
    ``coder`` verifier) so they cannot drift on what "no interpreter" means.
    """
    import sys

    if not getattr(sys, "frozen", False):
        return sys.executable, None
    exe = managed_python_exe()
    if exe is None:
        return None, (
            "no Python to run pytest in the packaged app — provision the managed runtime "
            "(Settings ▸ Tools, ~35 MB), then try again"
        )
    if "pytest" not in managed_runtime_distributions():
        return None, (
            "the managed Python runtime has no pytest — open the Plugins panel and hit "
            '"Install deps" on the plugin that needs it, then try again'
        )
    return str(exe), None


# ── per-plugin virtual environments (ADR 0085 amendment, 2026-11-08) ─────────────────
#
# A plugin that declares ``requires_runtimes: {python: {version, requirements}}`` gets its
# OWN venv at ``<plugin_store>/.venv`` — NOT the box-shared managed runtime above. Heavy,
# plugin-specific deps live there, provisioned with ``uv`` on the operator's Install click
# (never in-process on load: install ≠ code exec, ADR 0027), so they never load into the
# frozen host (ADR 0093 / 0058 D2). Part 2 of #4807 adds the spawn seam
# (``sdk.plugin_python`` / ``sdk.worker``); this half owns where the venv lives, whether it
# satisfies the manifest, and how it is provisioned.

#: Marker file inside a provisioned venv, recording the version + requirements it was built
#: for — so the loader tells a satisfying venv from a stale one WITHOUT spawning anything.
_VENV_MARKER = ".protoagent-plugin-runtime.json"
#: Override seam for tests / CI fixtures: ``PROTOAGENT_PLUGIN_PYTHON_<ID>`` points a plugin's
#: venv at a prepared directory, so a suite can drive the met/unmet gate without running uv.
#: ``<ID>`` is the plugin id upper-cased with every run of non-alphanumerics folded to ``_``.
_VENV_ENV_PREFIX = "PROTOAGENT_PLUGIN_PYTHON_"


@dataclass(frozen=True)
class VenvProvisionResult:
    """Outcome of :func:`provision_plugin_venv`. ``ok=True`` with ``python`` set on success;
    ``ok=False`` with a human ``error`` otherwise (uv missing, a ``uv venv`` / ``uv pip``
    failure). Reported, not raised, so the operator action surfaces it as banner text."""

    ok: bool
    python: Path | None = None
    error: str | None = None


def _check_plugin_id(plugin_id: str) -> str:
    """A bare plugin id, or ``ValueError`` — the same guard :func:`sdk.plugin_store` uses, so
    a venv path can never be redirected out of the plugin's own store."""
    pid = (plugin_id or "").strip()
    if not pid or ":" in pid or "/" in pid or "\\" in pid or pid in (".", ".."):
        raise ValueError(f"plugin_id must be a bare plugin id, got {plugin_id!r}")
    return pid


def _venv_env_key(plugin_id: str) -> str:
    return _VENV_ENV_PREFIX + re.sub(r"[^A-Z0-9]+", "_", plugin_id.upper())


def plugin_venv_dir(plugin_id: str) -> Path:
    """Where this plugin's venv lives: ``<plugin_store>/.venv`` (instance-scoped, ADR 0004),
    or the path in ``PROTOAGENT_PLUGIN_PYTHON_<ID>`` when that env var is set (tests / CI).

    Does not create anything — ``uv venv`` makes the directory on provisioning."""
    pid = _check_plugin_id(plugin_id)
    override = os.environ.get(_venv_env_key(pid))
    if override:
        return Path(override)
    from infra.paths import instance_paths

    return instance_paths().store(pid) / ".venv"


def _venv_python_exe(venv: Path) -> Path:
    """The interpreter inside a standard venv (what ``uv venv`` makes): POSIX
    ``<venv>/bin/python``, Windows ``<venv>/Scripts/python.exe``."""
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def requirements_hash(requirements) -> str:
    """A stable digest of a plugin's declared ``requirements`` — recorded in the venv marker
    and re-checked against the manifest, so a changed dep list re-provisions. Declared order
    is preserved (the manifest's is deterministic); blank entries are dropped."""
    norm = [str(r).strip() for r in (requirements or []) if str(r).strip()]
    return hashlib.sha256("\n".join(norm).encode("utf-8")).hexdigest()


def _read_marker(plugin_id: str) -> dict | None:
    marker = plugin_venv_dir(plugin_id) / _VENV_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def plugin_venv_python(plugin_id: str, version: str | None = None) -> Path | None:
    """The interpreter to spawn for this plugin, or ``None`` when the venv is missing (no
    interpreter on disk) or — when ``version`` is given — it was provisioned for a different
    ``major.minor``. Part 2's spawn seam resolves through here."""
    pid = _check_plugin_id(plugin_id)
    exe = _venv_python_exe(plugin_venv_dir(pid))
    if not exe.exists():
        return None
    if version is not None:
        marker = _read_marker(pid)
        if marker is None or str(marker.get("version")) != str(version):
            return None
    return exe


def plugin_venv_state(plugin_id: str, version: str, requirements) -> str:
    """Whether the plugin's venv satisfies the manifest, as one of ``"ok"`` (matches),
    ``"missing"`` (never provisioned), ``"version"`` (built for a different Python) or
    ``"stale"`` (the requirements hash drifted from the manifest).

    Marker-only — never spawns a process — so it is cheap on every load, and the operator's
    Install writes the marker that flips it to ``"ok"``."""
    pid = _check_plugin_id(plugin_id)
    marker = _read_marker(pid)
    if marker is None:
        return "missing"
    if str(marker.get("version")) != str(version):
        return "version"
    if str(marker.get("requirements_hash")) != requirements_hash(requirements):
        return "stale"
    return "ok"


def plugin_venv_satisfies(plugin_id: str, version: str, requirements) -> bool:
    """True when the plugin's venv matches the declared version AND requirements."""
    return plugin_venv_state(plugin_id, version, requirements) == "ok"


def uv_available() -> bool:
    """Whether ``uv`` (the provisioner) is resolvable on PATH — decides whether the
    runtime-missing banner can offer a one-click Install for a python runtime."""
    return shutil.which("uv") is not None


def _run_uv(cmd: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run a uv command with the parent env (proxies/certs apply), captured, no shell — the
    single seam a test stubs (``subprocess.run``) so it never shells out to a real uv."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)  # noqa: S603


def _uv_tail(proc: subprocess.CompletedProcess) -> str:
    return (proc.stderr or proc.stdout or "").strip()[-600:]


def provision_plugin_venv(plugin_id: str, version: str, requirements) -> VenvProvisionResult:
    """Create (or rebuild) the plugin's venv at ``<plugin_store>/.venv`` with ``uv venv
    --python <version>``, install its ``requirements`` into it with ``uv pip install``, and
    record the version + requirements hash so the loader sees it satisfied.

    Runs ONLY on the operator's Install action, never on load (ADR 0027: install ≠ code
    exec). Returns a :class:`VenvProvisionResult`; a missing ``uv`` or a uv failure is
    reported in ``error``, not raised, so the action can surface it as banner text."""
    pid = _check_plugin_id(plugin_id)
    uv = shutil.which("uv")
    if not uv:
        return VenvProvisionResult(
            ok=False,
            error="uv is not installed or not on PATH — install uv (https://docs.astral.sh/uv/) and retry.",
        )
    venv = plugin_venv_dir(pid)
    venv.parent.mkdir(parents=True, exist_ok=True)
    marker = venv / _VENV_MARKER
    # Drop a stale marker before rebuilding: a crash mid-build must not leave a venv that
    # still looks satisfied for the OLD version/requirements.
    try:
        marker.unlink()
    except OSError:
        pass
    created = _run_uv([uv, "venv", "--python", str(version), str(venv)], timeout=300)
    if created.returncode != 0:
        return VenvProvisionResult(ok=False, error=f"`uv venv` failed: {_uv_tail(created)}")
    exe = _venv_python_exe(venv)
    reqs = [str(r).strip() for r in (requirements or []) if str(r).strip()]
    if reqs:
        installed = _run_uv([uv, "pip", "install", "--python", str(exe), "--", *reqs], timeout=1800)
        if installed.returncode != 0:
            return VenvProvisionResult(ok=False, error=f"`uv pip install` failed: {_uv_tail(installed)}")
    # The venv dir exists after a real ``uv venv``; ensure it for a stubbed uv too, so the
    # marker (the loader's satisfied-signal) always lands where the next load reads it.
    venv.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"version": str(version), "requirements_hash": requirements_hash(reqs)}),
        encoding="utf-8",
    )
    log.info("[python] provisioned venv for plugin %s (Python %s, %d requirement(s))", pid, version, len(reqs))
    return VenvProvisionResult(ok=True, python=exe)
