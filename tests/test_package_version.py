"""`paths.package_version()` resolution — the anchored pyproject-first order (#1644)
plus the frozen-binary `_MEIPASS` fallback that keeps the desktop app from reporting
`0.0.0` (version-coherence Cross-cutting B). A wrong version here blinds the A2A card,
the fleet version handshake, runtime status, and the plugin `min_protoagent_version`
compat gate, so this must resolve the *fresh* version on every artifact:

- source checkout: the repo `pyproject.toml` must beat stale editable-install
  dist-info (editable installs only rewrite metadata on the next `uv sync`, so on a
  dev checkout the metadata lags every version bump — #1644);
- wheel/frozen installs: no pyproject ships at the anchor, so installed metadata
  answers — and the anchor must never wander into an unrelated project's pyproject.
"""

from __future__ import annotations

import importlib.metadata
import sys

from infra import paths


def _force_no_installed_metadata(monkeypatch):
    """Make ``importlib.metadata.version("protoagent")`` raise, so resolution falls
    through past the metadata fallback — the frozen-binary + Docker reality (the
    package is never pip-installed there)."""

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)


def _force_installed_metadata(monkeypatch, value: str):
    """Pretend the venv's dist-info says *value* for ``protoagent``."""
    monkeypatch.setattr(importlib.metadata, "version", lambda name: value)


def _anchor_module_at(monkeypatch, module_file):
    """Relocate the resolver's self-anchor: pretend ``infra/paths.py`` lives at
    *module_file* (the resolver reads its own ``__file__``, never the cwd)."""
    monkeypatch.setattr(paths, "__file__", str(module_file))


def test_repo_pyproject_beats_stale_editable_metadata(monkeypatch, tmp_path):
    # THE #1644 regression: on a source checkout the editable install's dist-info
    # says 0.72.0 while pyproject.toml says 0.80.0 — pyproject must win, or the
    # plugin gate refuses valid plugins and the A2A card advertises the old version.
    repo = tmp_path / "repo"
    (repo / "infra").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "protoagent"\nversion = "0.80.0"\n', encoding="utf-8")
    _anchor_module_at(monkeypatch, repo / "infra" / "paths.py")
    _force_installed_metadata(monkeypatch, "0.72.0")
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert paths.package_version() == "0.80.0"


def test_wheel_install_falls_back_to_metadata(monkeypatch, tmp_path):
    # Wheel install: the anchor is site-packages, which ships no pyproject.toml —
    # installed metadata is the truth there.
    site = tmp_path / ".venv" / "lib" / "python3.11" / "site-packages"
    (site / "infra").mkdir(parents=True)
    _anchor_module_at(monkeypatch, site / "infra" / "paths.py")
    _force_installed_metadata(monkeypatch, "0.75.0")
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert paths.package_version() == "0.75.0"


def test_never_reads_an_unrelated_projects_pyproject(monkeypatch, tmp_path):
    # protoAgent wheel-installed under someone else's project: `<their-project>/
    # pyproject.toml` exists up the directory tree, but the anchor is exact (the
    # package's own root), not an upward search — their version must not leak in.
    theirs = tmp_path / "their-project"
    site = theirs / ".venv" / "lib" / "python3.11" / "site-packages"
    (site / "infra").mkdir(parents=True)
    (theirs / "pyproject.toml").write_text('[project]\nname = "their-app"\nversion = "9.9.9"\n', encoding="utf-8")
    _anchor_module_at(monkeypatch, site / "infra" / "paths.py")
    _force_installed_metadata(monkeypatch, "0.75.0")
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    assert paths.package_version() == "0.75.0"


def test_reads_meipass_pyproject_when_frozen(monkeypatch, tmp_path):
    _force_no_installed_metadata(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "protoagent"\nversion = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert paths.package_version() == "9.9.9"


def test_frozen_without_bundled_pyproject_uses_metadata(monkeypatch, tmp_path):
    # A frozen build whose bundle step dropped pyproject.toml must still answer
    # from whatever metadata PyInstaller collected — not read some on-disk repo.
    _force_installed_metadata(monkeypatch, "0.77.0")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)  # empty dir

    assert paths.package_version() == "0.77.0"


def test_source_checkout_reads_repo_pyproject(monkeypatch):
    # Not frozen, no installed metadata → reads the repo-root pyproject.toml two
    # levels up from infra/paths.py (source checkout / `COPY .` image). Must be a
    # real version, not the 0.0.0 fallback.
    _force_no_installed_metadata(monkeypatch)
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    v = paths.package_version()
    assert v != "0.0.0" and v[0].isdigit()


def test_zero_fallback_only_when_nothing_resolves(monkeypatch, tmp_path):
    # Frozen, no metadata, and an empty _MEIPASS with no pyproject.toml → the explicit
    # last-resort fallback (a regression guard: if this ever fires on a real build,
    # the bundle step in build_sidecar.py dropped pyproject.toml).
    _force_no_installed_metadata(monkeypatch)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert paths.package_version() == "0.0.0"


def test_loader_gate_and_a2a_card_share_the_resolver(monkeypatch):
    # The #1644 contract: the plugin min_protoagent_version gate and the A2A agent
    # card must consume the SAME resolver so they can never disagree. Both import
    # it lazily from infra.paths, so patching the shared function moves both.
    import server.a2a as a2a
    from graph.plugins import loader

    monkeypatch.setattr(paths, "package_version", lambda: "7.7.7-shared")

    assert loader._host_version() == "7.7.7-shared"
    assert a2a._package_version() == "7.7.7-shared"


def test_min_version_gate_uses_fresh_pyproject_version(monkeypatch, tmp_path):
    # End-to-end through the gate: stale metadata (0.72.0) + fresh pyproject
    # (0.80.0) must ADMIT a plugin requiring 0.78.0 — the live #1644 failure mode.
    from graph.plugins import loader
    from graph.plugins.manifest import PluginManifest

    repo = tmp_path / "repo"
    (repo / "infra").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "protoagent"\nversion = "0.80.0"\n', encoding="utf-8")
    _anchor_module_at(monkeypatch, repo / "infra" / "paths.py")
    _force_installed_metadata(monkeypatch, "0.72.0")
    monkeypatch.setattr(sys, "frozen", False, raising=False)

    manifest = PluginManifest(
        id="spacetraders",
        name="SpaceTraders",
        path=tmp_path,
        min_protoagent_version="0.78.0",
    )
    assert loader._min_version_gate(manifest) is None
