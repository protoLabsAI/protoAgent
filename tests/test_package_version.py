"""`paths.package_version()` resolution — especially the frozen-binary `_MEIPASS`
fallback that keeps the desktop app from reporting `0.0.0` (version-coherence
Cross-cutting B). A `0.0.0` blinds the A2A card, the fleet version handshake, runtime
status, and the plugin `min_protoagent_version` compat gate, so this must resolve a
real version on every artifact.
"""

from __future__ import annotations

import importlib.metadata
import sys

from infra import paths


def _force_no_installed_metadata(monkeypatch):
    """Make ``importlib.metadata.version("protoagent")`` raise, so resolution falls
    through to the pyproject read — the frozen-binary + Docker reality (the package
    is never pip-installed there)."""

    def _raise(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)


def test_reads_meipass_pyproject_when_frozen(monkeypatch, tmp_path):
    _force_no_installed_metadata(monkeypatch)
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "protoagent"\nversion = "9.9.9"\n', encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    assert paths.package_version() == "9.9.9"


def test_source_checkout_reads_repo_pyproject(monkeypatch):
    # Not frozen, no installed metadata → reads pyproject.toml next to paths.py (the
    # repo root / `COPY .` image). Must be a real version, not the 0.0.0 fallback.
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
