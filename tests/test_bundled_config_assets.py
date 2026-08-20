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
import json
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


def _docs_seed_sources() -> set[str]:
    """The `docs/<section>` seed paths the bundled `docs` plugin's corpus actually needs,
    derived from `plugins/docs/corpus.py::SECTIONS` — single-sourced so a section added
    there is covered here without anyone remembering this file exists (same reasoning as
    the catalog glob above)."""
    from plugins.docs.corpus import SECTIONS

    return {f"docs/{section}" for section in SECTIONS}


def test_there_is_at_least_one_docs_section() -> None:
    """Guard the guard — an empty SECTIONS tuple would make every check below vacuous."""
    assert _docs_seed_sources(), "plugins/docs/corpus.py::SECTIONS is empty; this test would be vacuous"


@pytest.mark.parametrize("section", sorted(_docs_seed_sources()))
def test_docs_section_is_bundled_into_the_wheel(section: str) -> None:
    """`plugins/docs/corpus.py::docs_root()` resolves `docs/` beside the installed
    `plugins/` tree — on a real wheel install that's `site-packages/docs/`, which only
    exists if hatch_build.py actually force-includes it (#2626, the docs/ half of the
    #2624 bug: the comment promised it, `_SEEDS` never delivered it). Missing here ⇒
    docs_search/docs_read silently return nothing (`iter_docs` skips missing section
    dirs rather than erroring) instead of failing loudly."""
    assert section in _wheel_seed_sources(), (
        f"{section} is not in hatch_build.py::_SEEDS — the wheel (and the PyPI install) "
        f"would ship without it, and the docs corpus reader falls back to an EMPTY result "
        f"SILENTLY instead of erroring."
    )


@pytest.mark.parametrize("section", sorted(_docs_seed_sources()))
def test_docs_section_is_bundled_into_the_desktop_sidecar(section: str) -> None:
    """Same invariant as above, for the frozen desktop build."""
    assert section in _sidecar_bundled_sources(), (
        f"{section} is not in build_sidecar.py::BUNDLED_DATA — the frozen desktop sidecar "
        f"would ship without it, and the docs corpus reader falls back to an EMPTY result SILENTLY."
    )


def test_docs_seeds_exclude_dev_and_vitepress() -> None:
    """The docs seeds must stay a curated subsection list, not a whole-tree mapping —
    `docs/dev` (internal handoffs) and `docs/.vitepress` (build config/output, and the
    gitignored `.vitepress/dist`+`.vitepress/cache` can be a full built site if a release
    happens to be cut on a machine that's built the docs site locally) must never appear,
    because hatch_build.py's force-include has no exclude filtering of its own — the
    ONLY thing keeping them out is that nobody adds a broader `docs/...` entry than the
    five sections the corpus actually reads."""
    docs_sources = {s for s in _wheel_seed_sources() if s == "docs" or s.startswith("docs/")}
    offenders = {s for s in docs_sources if s == "docs" or s.startswith(("docs/dev", "docs/.vitepress"))}
    assert not offenders, (
        f"hatch_build.py::_SEEDS bundles {sorted(offenders)} — this sweeps internal docs "
        f"and/or the (gitignored, potentially large) built VitePress site into every wheel. "
        f"Bundle specific docs/<section> entries instead of the whole docs/ tree."
    )


def test_archetype_catalog_specifically() -> None:
    """The regression that prompted this file (#2010 shipped the catalog entry and
    the cowork SOUL preset, but never added the catalog to either asset list, so
    Cowork was unselectable on desktop and on any pip install)."""
    assert "config/archetype-catalog.json" in _wheel_seed_sources()
    assert "config/archetype-catalog.json" in _sidecar_bundled_sources()


def test_plugins_dir_is_bundled_into_the_wheel() -> None:
    """Core code imports directly from `plugins/` (e.g. `runtime/acp_runtime.py`'s
    `from plugins.coding_agent.acp_client import AcpClient`), so omitting the tree from
    the wheel isn't a missing extra — it's a `ModuleNotFoundError` on the first ACP-backed
    turn of any non-editable install (#2624). This stayed invisible because editable/source
    installs keep the repo root on `sys.path`, masking the omission entirely — only a real
    `pip install` from the built wheel exercises this path."""
    assert "plugins" in _wheel_seed_sources(), (
        "'plugins' is not in hatch_build.py::_SEEDS — a `pip install` from the built wheel "
        "would ship without it, and core code that imports from plugins/ (e.g. the ACP "
        "client factory) raises ModuleNotFoundError on the first real turn, not at install "
        "time or boot."
    )


def test_plugins_dir_is_bundled_into_the_desktop_sidecar() -> None:
    """Same invariant as above, for the frozen desktop build — already correct (this is
    the pattern #2624's wheel fix mirrors), pinned here so it can't silently regress."""
    assert "plugins" in _sidecar_bundled_sources(), (
        "'plugins' is not in build_sidecar.py::BUNDLED_DATA — the frozen desktop sidecar "
        "would ship without it, and PyInstaller's import-scan can't see plugins/ loaded by "
        "file path, so this can't be caught any other way."
    )


def test_project_manager_archetype_row() -> None:
    """#2178 ships the Project Manager archetype as catalog data only (ADR 0042) —
    pin the invariants the picker relies on: the id is unique (it's the RadioCard
    value + React key), its `soul_preset` resolves to a preset file that is really
    bundled, and `custom` is still the catch-all LAST row."""
    catalog = json.loads((CONFIG / "archetype-catalog.json").read_text())
    ids = [a["id"] for a in catalog["archetypes"]]

    assert ids.count("project-manager") == 1, f"'project-manager' must appear exactly once, got {ids}"

    (row,) = (a for a in catalog["archetypes"] if a["id"] == "project-manager")
    preset = CONFIG / "soul-presets" / f"{row['soul_preset']}.md"
    assert preset.is_file(), (
        f"archetype 'project-manager' points at soul_preset '{row['soul_preset']}' "
        f"but {preset} does not exist — the persona step would silently seed nothing."
    )

    # It's an advanced-tier archetype (ADR 0042 picker placement) — the picker files it
    # under the collapsed "Advanced" section rather than inline with Basic.
    assert row.get("tier") == "advanced", f"'project-manager' must be tier 'advanced', got {row.get('tier')!r}"

    assert ids[-1] == "custom", f"'custom' must stay LAST in the archetype list, got {ids}"


def test_design_system_archetype_row() -> None:
    """#2217 ships the Design System Engineer archetype as catalog data only (ADR
    0042) — same invariants as the project-manager row: unique id, `soul_preset`
    resolves to a really-bundled preset file, and `custom` stays the LAST row."""
    catalog = json.loads((CONFIG / "archetype-catalog.json").read_text())
    ids = [a["id"] for a in catalog["archetypes"]]

    assert ids.count("design-system") == 1, f"'design-system' must appear exactly once, got {ids}"

    (row,) = (a for a in catalog["archetypes"] if a["id"] == "design-system")
    preset = CONFIG / "soul-presets" / f"{row['soul_preset']}.md"
    assert preset.is_file(), (
        f"archetype 'design-system' points at soul_preset '{row['soul_preset']}' "
        f"but {preset} does not exist — the persona step would silently seed nothing."
    )

    # Advanced-tier archetype (ADR 0042) — collapsed under the picker's "Advanced" section.
    assert row.get("tier") == "advanced", f"'design-system' must be tier 'advanced', got {row.get('tier')!r}"

    assert ids[-1] == "custom", f"'custom' must stay LAST in the archetype list, got {ids}"


def test_social_marketing_archetype_is_held() -> None:
    """The Social Marketing archetype is HELD from the picker (operator call,
    2026-08-20: not ready for release). JSON has no comments, so 'commented out'
    is a sibling ``held`` list the reader never serves. This pins BOTH directions:
    the row must not ship in ``archetypes`` (it would become selectable again),
    and it must survive intact in ``held`` — soul preset and bundle still valid —
    so restoring it is one move, not an archaeology dig."""
    catalog = json.loads((CONFIG / "archetype-catalog.json").read_text())
    ids = [a["id"] for a in catalog["archetypes"]]

    assert "social-marketing" not in ids, (
        f"'social-marketing' is held from release — it must not be in archetypes, got {ids}"
    )

    held = [a["id"] for a in catalog.get("held") or []]
    assert held.count("social-marketing") == 1, (
        f"'social-marketing' must be parked exactly once in `held`, got {held}"
    )
    (row,) = (a for a in catalog["held"] if a["id"] == "social-marketing")
    preset = CONFIG / "soul-presets" / f"{row['soul_preset']}.md"
    assert preset.is_file(), (
        f"held archetype 'social-marketing' points at soul_preset '{row['soul_preset']}' "
        f"but {preset} does not exist — restoring the row would silently seed nothing."
    )
    assert row.get("bundle", "").startswith("https://github.com/protoLabsAI/"), (
        "the held row must keep its bundle URL so restoration is one move"
    )

    assert ids[-1] == "custom", f"'custom' must stay LAST in the archetype list, got {ids}"


def _sidecar_cli_hidden_imports() -> set[str]:
    """The `CLI_FORWARD_MODULES` list in build_sidecar.py, read statically (AST) —
    the dynamically-dispatched CLI modules the frozen build must hidden-import."""
    tree = ast.parse((ROOT / "apps" / "desktop" / "sidecar" / "build_sidecar.py").read_text())
    for node in ast.walk(tree):
        is_it = (
            isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "CLI_FORWARD_MODULES"
        ) or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "CLI_FORWARD_MODULES" for t in node.targets))
        if is_it:
            return {el.value for el in node.value.elts}  # type: ignore[attr-defined]
    raise AssertionError("could not find CLI_FORWARD_MODULES in build_sidecar.py")


def test_sidecar_bundles_every_forwarded_cli_module() -> None:
    """Every CLI verb `server.cli.dispatch` reaches via a dynamic `import_module`
    string (the `_FORWARD` table) MUST be a hidden-import in the sidecar build, or
    PyInstaller's static scan misses it and the verb dies with ModuleNotFoundError
    in the frozen app — #2136 (Fleet ▸ Add Agent → `plugin install` → no
    `graph.plugins.cli`), and the whole class: fleet / skills / runtime / operator-mcp.

    A new `_FORWARD` verb added without collecting it here reproduces the bug on the
    next desktop build; this pins the two lists together."""
    from server.cli import _FORWARD

    forwarded = {module for module, _func in _FORWARD.values()}
    collected = _sidecar_cli_hidden_imports()
    missing = sorted(forwarded - collected)
    assert not missing, (
        f"_FORWARD CLI module(s) {missing} are dynamically imported but NOT in "
        f"build_sidecar.py::CLI_FORWARD_MODULES — they'd 'ModuleNotFoundError' in the frozen app."
    )


def test_vendor_asset_routes_are_declared_public():
    """A plugin that serves vendored ES modules off its PUBLIC view prefix must exempt
    that subtree in ``public_paths``.

    ``manifest._view_public_paths`` auto-exempts a view's PAGE path, but an ES-module
    ``import`` carries no Authorization header any more than the iframe navigation does —
    so a gated ``/plugins/<id>/vendor/*`` 401s and the panel renders as a dead box. This
    stayed invisible while fleet members ran open on loopback; ADR 0089 D5 closed them, so
    the proxy now forwards these unauthenticated requests to a member that rejects them
    (notes + artifact both broke on sister agents this way).

    Guarding the CLASS, not the two instances: any future plugin that adds a vendor route
    without declaring it fails here rather than in someone's console.
    """
    import re
    from pathlib import Path

    import yaml

    plugins_dir = Path(__file__).parent.parent / "plugins"
    offenders: list[str] = []
    checked = 0

    for entry in sorted(plugins_dir.iterdir()):
        init, manifest_path = entry / "__init__.py", entry / "protoagent.plugin.yaml"
        if not (init.is_file() and manifest_path.is_file()):
            continue
        # Serves a vendor asset route off the public (non-/api) view prefix?
        if not re.search(r"""@\w+\.get\(\s*["']/vendor/""", init.read_text(encoding="utf-8")):
            continue
        checked += 1
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        pid = manifest.get("id") or entry.name
        wanted = f"/plugins/{pid}/vendor/"
        declared = [str(p) for p in (manifest.get("public_paths") or [])]
        if not any(d.startswith(wanted) or wanted.startswith(d) for d in declared):
            offenders.append(f"{entry.name} (needs public_paths entry {wanted!r}, has {declared})")

    assert checked, "no vendor-serving plugins found — has the route shape changed?"
    assert not offenders, "vendor assets gated behind auth — will 401 on a sister agent: " + "; ".join(offenders)


def test_cowork_archetype_requires_python_runtime() -> None:
    """The Cowork archetype's document skills route through execute_code, which on the
    desktop app needs the managed Python runtime (ADR 0094) — the catalog row declares
    it (#2186 follow-on) so the new-agent picker can warn at choose-time instead of the
    operator's first docx failing (the exact ADR 0092 first-run this exists for)."""
    catalog = json.loads((CONFIG / "archetype-catalog.json").read_text())
    (row,) = (a for a in catalog["archetypes"] if a["id"] == "cowork")
    assert row.get("requires") == ["python_runtime"]


def test_view_page_subresources_are_declared_public():
    """Every same-origin subresource a plugin's view PAGE references (script src,
    stylesheet href) must be auth-exempt: the page itself is public chrome
    (auto-exempted), but a <script src> load carries no Authorization header —
    a gated subresource 401s silently and the panel renders as a dead empty
    state (the 2026-08-20 artifact incident: #2822 moved the shell out of the
    inline HTML into shell.js and nothing exempted the new route; every
    bearer-gated instance showed 'No artifact yet' with data present).

    Guards the CLASS for the real-files view pattern: any plugin shipping an
    .html page that references plugin-namespace files must exempt each one.
    """
    import re
    from pathlib import Path

    import yaml

    plugins_dir = Path(__file__).parent.parent / "plugins"
    offenders: list[str] = []
    checked = 0

    for entry in sorted(plugins_dir.iterdir()):
        manifest_path = entry / "protoagent.plugin.yaml"
        if not manifest_path.is_file():
            continue
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        public = [str(p) for p in (manifest.get("public_paths") or [])]
        pid = manifest.get("id") or entry.name
        for page in sorted(entry.glob("*.html")):
            html = page.read_text(encoding="utf-8")
            for ref in re.findall(r'(?:src|href)="([^"]+)"', html):
                if ref.startswith(("http://", "https://", "data:", "#")):
                    continue
                if "'" in ref or "+" in ref or "{" in ref:
                    continue  # a JS-constructed URL inside an inline script, not a static ref
                resolved = ref if ref.startswith("/") else f"/plugins/{pid}/{ref}"
                if not resolved.startswith(f"/plugins/{pid}/"):
                    continue  # outside the namespace (e.g. /_ds/ kit) — core-owned
                checked += 1
                if not any(resolved.startswith(p) or resolved == p.rstrip("/") for p in public):
                    offenders.append(f"{pid}: {page.name} references {resolved} — not in public_paths")

    assert checked > 0, "expected at least one view-page subresource to check (artifact shell.js)"
    assert not offenders, "gated view subresources (401 = dead panel):\n" + "\n".join(offenders)
