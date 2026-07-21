"""Every curated `config/*catalog*.json` reaches BOTH packaged builds.

This bug has now landed three times, in the same shape each time:

  * `plugin-catalog.json` missing → Plugins ▸ Discover showed "0 official plugins"
  * `mcp-catalog.json` missing    → MCP ▸ Browse showed "no servers match"
  * `archetype-catalog.json` missing → the new-agent picker silently lost Cowork

It keeps recurring because the catalogs are *data*, and adding one means editing
the repo (which the Docker image copies wholesale, so Docker always works) while
forgetting two hand-maintained asset lists:

  * ``hatch_build.py::_SEEDS``                        — the wheel (PyPI, and the
    frozen desktop sidecar built from it)
  * ``apps/desktop/sidecar/build_sidecar.py::BUNDLED_DATA`` — PyInstaller

Both readers **fall back silently** rather than erroring — `_load_archetype_catalog`
returns Basic + Custom, the plugin catalog returns an empty directory. So the
packaged build looks healthy and just quietly offers less than the source tree
does, which is why every instance of this was found by a human noticing something
missing rather than by CI.

Asserting on a glob (not a hardcoded list) is the point: a catalog added tomorrow
is covered without anyone remembering this file exists.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


def _catalog_files() -> list[str]:
    """Every curated catalog in config/, repo-relative."""
    return sorted(f"config/{p.name}" for p in CONFIG.glob("*catalog*.json"))


def _wheel_seed_sources() -> set[str]:
    """The `src` keys of `hatch_build._SEEDS`, read statically (no hatchling import)."""
    tree = ast.parse((ROOT / "hatch_build.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_SEEDS":
            return {k.value for k in node.value.keys}  # type: ignore[attr-defined]
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_SEEDS" for t in node.targets):
            return {k.value for k in node.value.keys}  # type: ignore[attr-defined]
    raise AssertionError("could not find _SEEDS in hatch_build.py")


def _sidecar_bundled_sources() -> set[str]:
    """The first element of each `BUNDLED_DATA` (src, dest) tuple, read statically.

    Parsed rather than imported: build_sidecar.py is a PyInstaller driver and
    importing it pulls in build-time machinery a unit test shouldn't need.
    """
    tree = ast.parse((ROOT / "apps" / "desktop" / "sidecar" / "build_sidecar.py").read_text())
    for node in ast.walk(tree):
        target_is_bundled = (
            isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "BUNDLED_DATA"
        ) or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "BUNDLED_DATA" for t in node.targets))
        if target_is_bundled:
            return {el.elts[0].value for el in node.value.elts}  # type: ignore[attr-defined]
    raise AssertionError("could not find BUNDLED_DATA in build_sidecar.py")


def test_there_is_at_least_one_catalog() -> None:
    """Guard the guard — a glob that matches nothing would pass everything below."""
    assert _catalog_files(), "no config/*catalog*.json found; this test would be vacuous"


@pytest.mark.parametrize("catalog", _catalog_files())
def test_catalog_is_bundled_into_the_wheel(catalog: str) -> None:
    """Missing here ⇒ `pip install protolabs-agent` silently gets the fallback."""
    assert catalog in _wheel_seed_sources(), (
        f"{catalog} is not in hatch_build.py::_SEEDS — the wheel (and the PyPI install) "
        f"would ship without it, and the reader falls back SILENTLY instead of erroring."
    )


@pytest.mark.parametrize("catalog", _catalog_files())
def test_catalog_is_bundled_into_the_desktop_sidecar(catalog: str) -> None:
    """Missing here ⇒ the frozen desktop app silently gets the fallback."""
    assert catalog in _sidecar_bundled_sources(), (
        f"{catalog} is not in build_sidecar.py::BUNDLED_DATA — the frozen desktop "
        f"sidecar would ship without it, and the reader falls back SILENTLY."
    )


def test_the_two_asset_lists_agree_on_catalogs() -> None:
    """The lists are hand-maintained and their comments say 'keep in step'.

    Drift between them is the interesting failure: it ships a catalog to PyPI but
    not to desktop (or vice versa), so the bug reproduces on exactly one surface
    and looks like a platform quirk.
    """
    catalogs = set(_catalog_files())
    wheel = catalogs & _wheel_seed_sources()
    sidecar = catalogs & _sidecar_bundled_sources()
    assert wheel == sidecar, (
        "hatch_build._SEEDS and build_sidecar.BUNDLED_DATA disagree on catalogs — "
        f"wheel-only={sorted(wheel - sidecar)} sidecar-only={sorted(sidecar - wheel)}"
    )


def test_archetype_catalog_specifically() -> None:
    """The regression that prompted this file (#2010 shipped the catalog entry and
    the cowork SOUL preset, but never added the catalog to either asset list, so
    Cowork was unselectable on desktop and on any pip install)."""
    assert "config/archetype-catalog.json" in _wheel_seed_sources()
    assert "config/archetype-catalog.json" in _sidecar_bundled_sources()
