"""Seam guard for the ``graph/plugins/updates.py`` extraction (#3823).

``installer`` is the most-patched module in the suite (~225 ``monkeypatch.setattr
(installer, ...)`` sites). The update-check code moved to ``updates.py`` and is
re-exported from ``installer`` — but a re-export only keeps names RESOLVING. For the
patches to keep INTERCEPTING, the moved code must look up every installer-owned
collaborator on the installer module at call time, never through a from-import or a
bare global. These tests fail loudly if that rule is broken, instead of letting a
patched test pass silently against the real function.
"""

from __future__ import annotations

import ast
import builtins
from pathlib import Path

from graph.plugins import installer, updates

_UPDATES_SRC = Path(updates.__file__).read_text(encoding="utf-8")
_MOVED = (
    "_ls_remote_sha",
    "_ls_remote_tags",
    "_lsremote_cache",
    "_lstags_cache",
    "check_plugin_update",
    "check_updates",
    "check_bundle_updates",
)


def test_updates_never_from_imports_installer_names():
    tree = ast.parse(_UPDATES_SRC)
    bad = [
        f"line {n.lineno}: from {n.module} import {', '.join(a.name for a in n.names)}"
        for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) and n.module == "graph.plugins.installer"
    ]
    assert not bad, "import the installer MODULE lazily and call through it: " + "; ".join(bad)


def test_updates_uses_no_installer_global_by_bare_name():
    """Every bare name ``updates.py`` loads must be its own global, a local, a builtin or
    a stdlib module — never a name that only ``installer`` defines (that would mean a
    stale copy or a from-import bypassing ``monkeypatch.setattr(installer, ...)``)."""
    tree = ast.parse(_UPDATES_SRC)
    own = set(vars(updates))
    local: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.arg):
            local.add(n.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            local.add(n.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            local |= {a.asname or a.name.split(".")[0] for a in n.names}
    installer_only = set(vars(installer)) - own - local - set(dir(builtins))
    leaked = sorted(
        {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} & installer_only
    )
    assert not leaked, f"reach these through _inst(), not by bare name: {leaked}"


def test_installer_re_exports_are_the_same_objects():
    for name in _MOVED:
        assert getattr(installer, name) is getattr(updates, name), name


def test_patches_on_installer_intercept_the_moved_code(monkeypatch):
    """Behavioral proof: patch collaborators on ``installer`` and drive ``updates``."""
    installer._lsremote_cache.clear()
    installer._lstags_cache.clear()
    calls: list[tuple] = []

    def fake_git(*args, **kwargs):
        calls.append(args)
        return ("b" * 40) + "\tHEAD"

    monkeypatch.setattr(installer, "_git", fake_git)
    monkeypatch.setattr(installer, "_git_auth_env", lambda url: {})
    monkeypatch.setattr(installer, "bundled_superseding", lambda pid, url: None)
    row = {
        "id": "demo",
        "source_url": "https://example.com/demo.git",
        "requested_ref": "main",
        "resolved_sha": "a" * 40,
    }
    monkeypatch.setattr(installer, "_lock_rows_by_id", lambda: {"demo": row})
    monkeypatch.setattr(installer, "_read_lock", lambda: {"plugins": [row], "bundles": [dict(row, id="stack")]})
    try:
        assert updates.check_updates()[0]["behind"] is True
        assert calls and calls[0][0] == "ls-remote"

        # installer.check_plugin_update is what check_updates / check_bundle_updates
        # (and installer's own _install_bundle) call — a patch there must intercept.
        monkeypatch.setattr(installer, "check_plugin_update", lambda e: {"id": e["id"], "patched": True})
        assert updates.check_updates() == [{"id": "demo", "patched": True}]
        assert installer.check_updates() == [{"id": "demo", "patched": True}]
        assert installer.check_bundle_updates() == [{"id": "stack", "patched": True}]
    finally:
        installer._lsremote_cache.clear()
        installer._lstags_cache.clear()


def test_ttl_knob_patched_on_installer_is_honored(monkeypatch):
    installer._lsremote_cache.clear()
    n = {"git": 0}

    def fake_git(*a, **k):
        n["git"] += 1
        return ("c" * 40) + "\tHEAD"

    monkeypatch.setattr(installer, "_git", fake_git)
    monkeypatch.setattr(installer, "_git_auth_env", lambda url: {})
    monkeypatch.setattr(installer, "_LSREMOTE_TTL_S", 0.0)
    try:
        updates._ls_remote_sha("https://example.com/x.git", "")
        updates._ls_remote_sha("https://example.com/x.git", "")
        assert n["git"] == 2  # TTL 0 (patched on installer) → no cache hit
    finally:
        installer._lsremote_cache.clear()
