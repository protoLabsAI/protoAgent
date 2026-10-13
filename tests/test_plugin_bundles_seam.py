"""Seam guard for the ``graph/plugins/bundles.py`` extraction (#3849).

Bundle install/uninstall moved out of ``installer`` and is re-exported there. The suite
patches collaborators on ``installer`` (``install``, ``_read_lock``, ``_write_lock``,
``check_plugin_update``, ``uninstall``, ``bundle_entry``, …). A re-export only keeps
names RESOLVING; for those patches to keep INTERCEPTING, the moved code must look up
every installer-namespace collaborator — its own re-exported siblings included — on the
installer module at call time. ``installer`` stays the ONE patch point: a patch on
``bundles`` would miss every caller (``installer.install`` calls ``_install_bundle`` by
bare name; ``ops``/``cli`` call ``installer.uninstall_bundle``; the moved code calls
``_inst().<name>``), so that is what the suite scan below forbids.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

from graph.plugins import bundles, installer
from tests._seam_scan import stale_patches

_BUNDLES_SRC = Path(bundles.__file__).read_text(encoding="utf-8")
_MOVED = (
    "BUNDLE_FILENAME",
    "load_bundle",
    "CONFIG_INPUT_TYPES",
    "CONFIG_INPUT_RESERVED_SECTIONS",
    "_CONFIG_INPUT_KEY_RE",
    "normalize_config_inputs",
    "coerce_config_input_value",
    "bundle_config_overlay",
    "_ARCHETYPE_KEYS",
    "_checked_archetype_block",
    "_install_bundle",
    "bundle_entry",
    "_bundle_ownership",
    "_exclusively_owned",
    "orphaned_bundle_members",
    "uninstall_bundle",
)
# Module-level constants/data the moved code may read by bare name (their one home is
# bundles.py; nothing patches them). Every moved FUNCTION is reached via ``_inst()``.
_BARE_OK = {"BUNDLE_FILENAME", "CONFIG_INPUT_TYPES", "CONFIG_INPUT_RESERVED_SECTIONS", "_CONFIG_INPUT_KEY_RE", "_ARCHETYPE_KEYS"}


def test_bundles_never_from_imports_installer_names():
    tree = ast.parse(_BUNDLES_SRC)
    bad = [
        f"line {n.lineno}: from {n.module} import {', '.join(a.name for a in n.names)}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "graph.plugins.installer"
    ]
    assert not bad, "import the installer MODULE lazily and call through it: " + "; ".join(bad)


def test_bundles_uses_no_installer_name_by_bare_name():
    """Every bare name ``bundles.py`` loads must be a local, a builtin, a stdlib import or
    one of its own constants — never an installer-namespace function (installer-only, or
    a moved sibling that installer re-exports): that would bypass
    ``monkeypatch.setattr(installer, ...)``."""
    tree = ast.parse(_BUNDLES_SRC)
    local: set[str] = {"_inst"}
    for n in ast.walk(tree):
        if isinstance(n, ast.arg):
            local.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            local |= {a.asname or a.name.split(".")[0] for a in n.names}
    installer_names = (set(vars(installer)) - local - set(dir(builtins)) - _BARE_OK) | (set(_MOVED) - _BARE_OK)
    leaked = sorted(
        {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} & installer_names
    )
    assert not leaked, f"reach these through _inst(), not by bare name: {leaked}"


def test_installer_re_exports_are_the_same_objects():
    for name in _MOVED:
        assert getattr(installer, name) is getattr(bundles, name), name


def test_no_test_patches_a_moved_name_on_bundles():
    """``installer`` is the patch point; a patch on ``bundles`` intercepts nothing."""
    stale = stale_patches("graph.plugins.bundles", None)
    assert not stale, "patch these on graph.plugins.installer instead: " + "; ".join(stale)


def test_patches_on_installer_intercept_install_bundle(monkeypatch):
    """Behavioral proof: patch collaborators on ``installer`` and drive ``bundles``."""
    lock: dict = {"plugins": [], "bundles": []}
    installs: list[tuple] = []
    audits: list[str] = []
    monkeypatch.setattr(installer, "_read_lock", lambda: {k: list(v) for k, v in lock.items()})
    monkeypatch.setattr(installer, "_write_lock", lambda data: lock.update(data))
    monkeypatch.setattr(installer, "_audit", lambda action, *a, **k: audits.append(action))
    monkeypatch.setattr(installer, "superseding_plugin", lambda url: None)
    monkeypatch.setattr(installer, "check_plugin_update", lambda e: {"latest_ref": "v0.1.9"})

    def fake_install(url, ref, **kw):
        installs.append((url, ref, kw["by"]))
        return {"id": url.rsplit("/", 1)[1]}

    monkeypatch.setattr(installer, "install", fake_install)
    bundle = {"id": "stack", "plugins": [{"id": "a", "url": "https://example.com/a", "ref": "v0.1.0"}]}
    res = bundles._install_bundle(bundle, "https://example.com/stack", "f" * 40, None, force=False, by="t", allow=None)
    assert res["installed"] == [{"id": "a"}]
    # check_plugin_update (patched on installer) chased the floor to the compatible tag.
    assert installs == [("https://example.com/a", "v0.1.9", "bundle:stack")]
    assert audits == ["install-bundle"]
    assert [b["id"] for b in lock["bundles"]] == ["stack"]

    # A patched moved sibling on installer intercepts the moved caller too.
    monkeypatch.setattr(installer, "normalize_config_inputs", lambda bid, raw, strict=True: [{"key": "patched"}])
    assert bundles._install_bundle(bundle, "u", "f" * 40, None, force=False, by="t", allow=None)["config_inputs"] == [
        {"key": "patched"}
    ]


def test_patches_on_installer_intercept_uninstall_bundle(monkeypatch):
    lock = {
        "plugins": [{"id": "a", "by": "bundle:stack"}, {"id": "b", "by": "direct"}],
        "bundles": [{"id": "stack", "plugins": ["a", "b", "gone"]}],
    }
    removed: list[str] = []
    monkeypatch.setattr(installer, "_read_lock", lambda: {k: [dict(r) for r in v] for k, v in lock.items()})
    monkeypatch.setattr(installer, "_write_lock", lambda data: lock.update(data))
    monkeypatch.setattr(installer, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(installer, "uninstall", lambda pid, purge=False: removed.append(pid) or {"id": pid})
    rep = bundles.uninstall_bundle("stack")
    assert removed == ["a"] and rep["removed_members"] == ["a"]
    assert rep["kept"] == ["b"] and rep["skipped_missing"] == ["gone"]
    assert lock["bundles"] == []

    lock["bundles"] = [{"id": "stack", "plugins": ["a"]}]
    monkeypatch.setattr(installer, "_exclusively_owned", lambda *a: False)
    assert bundles.uninstall_bundle("stack")["kept"] == ["a"]
