"""Pip-less wheel installer for frozen-app plugin deps (ADR 0093 P1 — pure wheels).

ADR 0058 D2 refuses a frozen-app plugin whose ``requires_pip`` isn't already
importable in the read-only PyInstaller bundle, because the frozen app has no
``pip`` (``sys.executable`` is the app binary) and the bundle is read-only. ADR
0058 D1 already closed the *git* gap with an httpx archive fetch; this closes the
*pip* gap the same way: resolve a package via PyPI's JSON API, download its
**wheel** (a zip), and unpack it into a writable per-instance deps dir that boot
prepends to ``sys.path`` (``graph.plugins.loader``) — so the plugin's own module
imports resolve in the host process.

P1 is **pure-Python wheels only** (``*-none-any``): no compiler, no build backend,
no platform-tag matching. A package that ships only an sdist or platform wheels
keeps ADR 0058 D2's refusal, naming itself (that's ADR 0093 P2). Transitive deps
are resolved from PyPI's ``requires_dist`` with real marker/specifier evaluation.

Safety (ADR 0093 D1/D6): opt-in only (the caller gates on
``plugins.allow_unbundled_deps``); the ADR 0058 spec rails (``_validate_pip_specs``)
still apply upstream; every ``(name, version, wheel sha256)`` is pinned in
``plugins.lock`` and a mismatched hash aborts. This is code-that-runs-on-import,
not a sandbox (ADR 0071) — the new surface over installing a plugin at all is its
*transitive* deps, which the lock's hashes pin.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Callable

from infra.paths import instance_paths

log = logging.getLogger("protoagent.plugins.wheel_installer")

_PYPI = "https://pypi.org/pypi"
_MAX_DEPTH = 6  # a transitive chain deeper than this is almost certainly a resolver bug


class WheelInstallError(RuntimeError):
    """Resolution / download / unpack failure — the caller wraps it as an InstallError."""


def plugin_deps_root() -> Path:
    """Per-instance root for unpacked wheel deps — a sibling of the live plugins dir
    (``instance_root/plugin-deps``), so instances don't share a mutable dep set (ADR
    0093 D2) and it's discovered/removed alongside the plugin it belongs to."""
    return instance_paths().plugins_dir.parent / "plugin-deps"


def plugin_deps_dir(plugin_id: str) -> Path:
    """Per-plugin deps dir (``…/plugin-deps/<id>``) — ``uninstall --purge`` drops
    exactly this dir, and boot prepends each existing one to ``sys.path``."""
    return plugin_deps_root() / plugin_id


def existing_deps_dirs() -> list[Path]:
    """Every provisioned per-plugin deps dir — the set boot prepends to ``sys.path``."""
    root = plugin_deps_root()
    return [d for d in root.iterdir() if d.is_dir()] if root.exists() else []


def prepend_to_syspath(path: Path) -> None:
    """Put ``path`` at the FRONT of ``sys.path`` (idempotent) so a just-unpacked dep
    imports without a restart — the live-adopt the managed Node runtime does for PATH."""
    s = str(path)
    if s not in sys.path:
        sys.path.insert(0, s)


# ── PyPI JSON + pure-wheel selection ──────────────────────────────────────────


def _pypi_json(name: str, version: str | None = None) -> dict:
    # Reuse the installer's hardened httpx GET (redirects, timeout, InstallError text).
    from graph.plugins.installer import _http_get

    url = f"{_PYPI}/{name}/json" if version is None else f"{_PYPI}/{name}/{version}/json"
    return _http_get(url).json()


def _is_pure_wheel(filename: str) -> bool:
    """A wheel is pure-Python iff its ABI tag is ``none`` and its platform tag is
    ``any`` — ``{dist}-{ver}(-{build})?-{pytag}-{abitag}-{platform}.whl``. That's the
    only kind P1 can install (no compiled extension, no platform match needed)."""
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    return len(parts) >= 3 and parts[-2] == "none" and parts[-1] == "any"


def _select(name: str, specifier: str) -> tuple[str, dict]:
    """(version, wheel-file-record) — the highest non-prerelease version satisfying
    ``specifier`` that ships a pure wheel. Raises when none qualifies (P2 territory)."""
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    spec = SpecifierSet(specifier or "")
    releases: dict = _pypi_json(name).get("releases", {}) or {}
    best: tuple[Version, dict] | None = None
    saw_version = False
    for ver, files in releases.items():
        try:
            v = Version(ver)
        except InvalidVersion:
            continue
        if v.is_prerelease or (specifier and v not in spec):
            continue
        saw_version = True
        pure = next((f for f in files if f.get("packagetype") == "bdist_wheel" and _is_pure_wheel(f.get("filename", ""))), None)
        if pure and (best is None or v > best[0]):
            best = (v, pure)
    if best is None:
        why = (
            "no version matches the specifier" if not saw_version else
            "only sdist / platform wheels on PyPI — a pure-Python wheel is required (ADR 0093 P1)"
        )
        raise WheelInstallError(f"{name}{specifier or ''}: {why}")
    return str(best[0]), best[1]


def _deps_of(name: str, version: str) -> list[str]:
    """A release's ``requires_dist`` (raw PEP 508 strings), or [] when unspecified."""
    return _pypi_json(name, version).get("info", {}).get("requires_dist") or []


def resolve(requirements: list[str], *, already_satisfied: Callable[[str], bool]) -> list[tuple[str, str, dict]]:
    """Transitively resolve ``requirements`` to a flat, deduped install list of
    ``(name, version, wheel-record)`` — pure wheels only.

    Evaluates PEP 508 markers against the frozen runtime (dropping inapplicable +
    extra-gated deps), honors version specifiers, short-circuits anything already
    importable in the bundle (``already_satisfied`` — never re-fetch what ADR 0092 or
    the core ship), and guards against cycles + runaway depth. Any package that can't
    be resolved fails the whole install, naming itself (ADR 0093 D5)."""
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name

    base_env = {**default_environment(), "extra": ""}
    resolved: dict[str, tuple[str, str, dict]] = {}
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(r, 0) for r in requirements if r and r.strip()]
    while queue:
        spec_str, depth = queue.pop(0)
        if depth > _MAX_DEPTH:
            raise WheelInstallError(f"dependency chain exceeds depth {_MAX_DEPTH} at {spec_str!r} (possible cycle)")
        try:
            req = Requirement(spec_str)
        except Exception as exc:  # noqa: BLE001 — a malformed requires_dist entry
            raise WheelInstallError(f"unparseable requirement {spec_str!r}: {exc}") from exc
        # Marker (incl. extras): an ``extra == "x"`` clause is False under extra="" so
        # extra-only deps drop — P1 installs the base package, not its optional extras.
        if req.marker is not None and not req.marker.evaluate(base_env):
            continue
        key = canonicalize_name(req.name)
        if key in visited:
            continue
        visited.add(key)
        if already_satisfied(req.name):  # bundle / core already ships it → no network
            continue
        version, wheel = _select(req.name, str(req.specifier))
        resolved[key] = (req.name, version, wheel)
        for dep in _deps_of(req.name, version):
            queue.append((dep, depth + 1))
    return list(resolved.values())


# ── download + verify + unpack + lock ─────────────────────────────────────────


def _download_verified(wheel: dict) -> bytes:
    """Download a wheel over httpx and verify its sha256 against the PyPI-declared
    digest — a tamper-evident fetch (a mismatched mirror aborts, ADR 0093 D6)."""
    from graph.plugins.installer import _http_get

    expected = ((wheel.get("digests") or {}).get("sha256") or "").lower()
    if not expected:
        raise WheelInstallError(f"{wheel.get('filename')!r}: PyPI gave no sha256 digest — refusing an unpinnable wheel")
    data = _http_get(wheel["url"]).content
    got = hashlib.sha256(data).hexdigest()
    if got != expected:
        raise WheelInstallError(f"{wheel.get('filename')!r}: sha256 {got} != pinned {expected} — refusing")
    return data


def _unpack_wheel(data: bytes, dest: Path) -> None:
    """Unpack a (pure) wheel's members into ``dest`` — path-traversal-safe. A pure
    wheel's importable packages + ``.dist-info`` sit at the archive root, so the deps
    dir on ``sys.path`` makes them importable. (``.data`` scripts/headers are a
    platform-install concern P1 pure-lib deps don't need.)"""
    dest = dest.resolve()
    with zipfile.ZipFile(BytesIO(data)) as zf:
        for member in zf.namelist():
            if member.endswith("/"):
                continue
            target = (dest / member).resolve()
            if target != dest and not str(target).startswith(str(dest) + "/"):
                raise WheelInstallError(f"unsafe path in wheel: {member!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(member))


def install(plugin_id: str, requirements: list[str], *, already_satisfied: Callable[[str], bool] | None = None) -> list[str]:
    """Resolve + download + unpack ``requirements`` (and their pure-wheel transitive
    deps) into the plugin's deps dir, pin each in ``plugins.lock``, and prepend the dir
    to ``sys.path`` so they import without a restart. Returns ``name==version`` for
    each dep actually installed. Raises ``WheelInstallError`` on any resolution/fetch
    failure — nothing partial is recorded in the lock."""
    sat = already_satisfied or (lambda _n: False)
    plan = resolve(requirements, already_satisfied=sat)
    dest = plugin_deps_dir(plugin_id)
    dest.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    pins: list[tuple[str, str, str]] = []
    for name, version, wheel in plan:
        _unpack_wheel(_download_verified(wheel), dest)
        installed.append(f"{name}=={version}")
        pins.append((name, version, wheel["digests"]["sha256"]))
    if pins:
        _record_lock(plugin_id, pins)
    prepend_to_syspath(dest)
    log.info("[wheel] installed %d dep(s) for %s into %s", len(installed), plugin_id, dest)
    return installed


def _record_lock(plugin_id: str, pins: list[tuple[str, str, str]]) -> None:
    """Pin the resolved deps under the plugin's ``plugins.lock`` entry
    (``deps: [{name, version, sha256}]``) — reproducible + tamper-evident re-install.
    Best-effort: a lock write failure logs but never loses the working install."""
    try:
        import json

        from graph.plugins.installer import lock_path

        lp = lock_path()
        lock = json.loads(lp.read_text()) if lp.exists() else {}
        entry = lock.get(plugin_id)
        if not isinstance(entry, dict):
            entry = {} if entry is None else {"_": entry}
        entry["deps"] = [{"name": n, "version": v, "sha256": h} for n, v, h in pins]
        lock[plugin_id] = entry
        lp.parent.mkdir(parents=True, exist_ok=True)
        lp.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 — the lock is provenance, not the install itself
        log.warning("[wheel] failed to record deps for %s in plugins.lock", plugin_id, exc_info=True)
