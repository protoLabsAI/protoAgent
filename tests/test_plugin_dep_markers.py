"""PEP 508 environment markers in a plugin's ``requires_pip``.

The Terminal plugin declares ``pywinpty>=2.0; sys_platform == 'win32'``. The dep check
used to read only the NAME, so on macOS/Linux pywinpty was "missing" forever: Install deps
answered "installed" (its pre-check, ``_spec_satisfied``, did honour the marker) while the
banner and the Plugins row kept asking for it. Every "what's missing" answer now evaluates
the marker the same way — false → not required here, unparsable → the old name-only answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.plugins import installer
from graph.plugins.manifest import load_manifest

# The Terminal plugin's REAL dependency declaration, verbatim from
# github.com/protoLabsAI/terminal-plugin protoagent.plugin.yaml (v0.4.1): Linux/macOS use
# the stdlib pty (no deps); only Windows needs pywinpty.
TERMINAL_MANIFEST = """\
id: terminal
name: Terminal
version: 0.4.1
repository: https://github.com/protoLabsAI/terminal-plugin
requires_pip:
  - "pywinpty>=2.0; sys_platform == 'win32'"
"""

NOPE = "definitely-not-a-real-pkg-xyz"


def _on(monkeypatch, platform: str) -> None:
    """Evaluate markers as if on ``platform`` (a ``sys.platform`` value). packaging caches
    the process environment, so this goes through the installer's override seam; the
    marker parsing and evaluation are packaging's real ones."""
    monkeypatch.setattr(installer, "_marker_environment", lambda: {"sys_platform": platform})


# ── _dep_applies / applicable_deps ─────────────────────────────────────────────


def test_no_marker_always_applies():
    assert installer._dep_applies("httpx>=0.27")
    assert installer._dep_applies("pkg[extra]>=1")


def test_platform_marker_is_evaluated(monkeypatch):
    _on(monkeypatch, "darwin")
    assert not installer._dep_applies("pywinpty>=2.0; sys_platform == 'win32'")
    assert installer._dep_applies("pyobjc>=10; sys_platform == 'darwin'")
    _on(monkeypatch, "win32")
    assert installer._dep_applies("pywinpty>=2.0; sys_platform == 'win32'")


def test_python_version_marker_is_evaluated():
    assert installer._dep_applies(f"{NOPE}; python_version >= '3.0'")
    assert not installer._dep_applies(f"{NOPE}; python_version < '3.0'")


@pytest.mark.parametrize("spec", [f"{NOPE}; this is not a marker", f"{NOPE} >>> 1; sys_platform == 'win32'"])
def test_an_unparsable_spec_keeps_the_name_only_answer(spec):
    # Unparsable → applies (today's behaviour): the name still gets checked and reported.
    assert installer._dep_applies(spec)
    assert installer._deps_satisfied([spec]) == (False, [NOPE])


def test_applicable_deps_filters_in_order(monkeypatch):
    _on(monkeypatch, "linux")
    specs = ["a>=1", "b; sys_platform == 'win32'", "c; sys_platform == 'linux'"]
    assert installer.applicable_deps(specs) == ["a>=1", "c; sys_platform == 'linux'"]


# ── _deps_satisfied: the loader gap, the /installed row and the frozen paths ────


def test_a_windows_only_dep_is_never_missing_off_windows(monkeypatch):
    _on(monkeypatch, "darwin")
    assert installer._deps_satisfied([f"{NOPE}>=2.0; sys_platform == 'win32'"]) == (True, [])


def test_a_platform_dep_that_applies_here_and_is_absent_is_missing(monkeypatch):
    _on(monkeypatch, "darwin")
    ok, missing = installer._deps_satisfied([f"{NOPE}>=1; sys_platform == 'darwin'"])
    # The CLEAN dist name, never the spec string with its marker.
    assert not ok and missing == [NOPE]


def test_python_version_markers_gate_missing():
    assert installer._deps_satisfied([f"{NOPE}; python_version < '3.0'"]) == (True, [])
    assert installer._deps_satisfied([f"{NOPE}; python_version >= '3.0'"]) == (False, [NOPE])


def test_spec_satisfied_and_deps_satisfied_agree_on_markers(monkeypatch):
    """The two used to disagree — that disagreement WAS the bug."""
    _on(monkeypatch, "linux")
    for spec in (f"{NOPE}; sys_platform == 'win32'", f"{NOPE}; sys_platform == 'linux'", "httpx"):
        assert installer._spec_satisfied(spec) == installer._deps_satisfied([spec])[0], spec


def test_frozen_dep_check_ignores_a_marker_excluded_dep(monkeypatch):
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())
    _on(monkeypatch, "darwin")
    assert installer._deps_satisfied([f"{NOPE}; sys_platform == 'win32'"]) == (True, [])


def test_frozen_install_never_sends_a_marker_excluded_spec_to_the_runtime(monkeypatch):
    """Two specs of one name with complementary markers: only the one that applies here
    may reach the managed runtime's pip."""
    import infra.python_runtime as pr
    import runtime.python_install as pi

    _on(monkeypatch, "darwin")
    monkeypatch.setattr(pr, "managed_python_exe", lambda: Path("/fake/runtime/bin/python3"))
    got: dict = {}
    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", lambda reqs, **k: got.setdefault("reqs", reqs))
    monkeypatch.setattr(installer, "_audit", lambda *a, **k: None)
    specs = [f"{NOPE}>=1; sys_platform == 'win32'", f"{NOPE}>=2; sys_platform != 'win32'"]
    ok, missing = installer._deps_satisfied(specs)
    assert not ok and missing == [NOPE]
    installer._frozen_install_missing_deps("p", specs, missing)
    assert got["reqs"] == [f"{NOPE}>=2; sys_platform != 'win32'"]


# ── the real Terminal plugin manifest ───────────────────────────────────────────


def _terminal(tmp_path: Path):
    d = tmp_path / "terminal"
    d.mkdir()
    (d / "protoagent.plugin.yaml").write_text(TERMINAL_MANIFEST, encoding="utf-8")
    m = load_manifest(d)
    assert m is not None and m.requires_pip == ["pywinpty>=2.0; sys_platform == 'win32'"]
    return m


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_terminal_plugin_needs_nothing_on_macos_or_linux(tmp_path, monkeypatch, platform):
    """Josh's report: 'Terminal: pywinpty>=2.0; sys_platform == win32' kept showing on macOS
    after Install deps said it was done. Built from the plugin's actual manifest."""
    m = _terminal(tmp_path)
    _on(monkeypatch, platform)
    real = installer._importable
    monkeypatch.setattr(installer, "_importable", lambda n: n != "pywinpty" and real(n))
    assert installer._deps_satisfied(m.requires_pip, m.pip_scopes) == (True, [])
    assert installer.missing_deps_detail(m) == []
    assert installer.applicable_deps(m.requires_pip) == []


def test_terminal_plugin_still_needs_pywinpty_on_windows(tmp_path, monkeypatch):
    m = _terminal(tmp_path)
    _on(monkeypatch, "win32")
    real = installer._importable
    monkeypatch.setattr(installer, "_importable", lambda n: n != "pywinpty" and real(n))
    assert installer._deps_satisfied(m.requires_pip, m.pip_scopes) == (False, ["pywinpty"])
    assert installer.missing_deps_detail(m) == [
        {"name": "pywinpty", "spec": "pywinpty>=2.0; sys_platform == 'win32'", "optional": False}
    ]


def test_terminal_plugin_raises_no_banner_on_macos(tmp_path, monkeypatch):
    """The loader's deps gap (the banner) and ``deps_missing`` (the Plugins row) for the
    real Terminal manifest on macOS: nothing."""
    from graph.plugins import loader, setup_gaps

    setup_gaps.reset()
    m = _terminal(tmp_path)
    _on(monkeypatch, "darwin")
    assert loader._report_deps_gap(m) == []
    assert not [g for g in setup_gaps.active() if g["key"] == loader.DEPS_GAP_KEY]
    setup_gaps.reset()


# ── missing_deps_detail: what the install-time dialog lists ─────────────────────


def test_missing_deps_detail_lists_hard_then_optional_specs(tmp_path, monkeypatch):
    _on(monkeypatch, "darwin")
    d = tmp_path / "p"
    d.mkdir()
    (d / "protoagent.plugin.yaml").write_text(
        "id: p\nname: P\nrequires_pip:\n"
        f"  - \"{NOPE}-a>=1\"\n"
        f"  - \"{NOPE}-win; sys_platform == 'win32'\"\n"
        "  - \"httpx>=0.1\"\n"
        f"  - {{ pkg: \"{NOPE}-b>=2\", optional: true }}\n",
        encoding="utf-8",
    )
    m = load_manifest(d)
    assert installer.missing_deps_detail(m) == [
        {"name": f"{NOPE}-a", "spec": f"{NOPE}-a>=1", "optional": False},
        {"name": f"{NOPE}-b", "spec": f"{NOPE}-b>=2", "optional": True},
    ]


# ── install route: deps_needed drives the console's install-time dialog ────────


def test_install_route_reports_deps_needed_with_specs_source_and_target(monkeypatch, tmp_path):
    from tests.test_plugin_routes import _client, _wire

    _on(monkeypatch, "darwin")
    d = tmp_path / "widgets"
    d.mkdir()
    (d / "protoagent.plugin.yaml").write_text(
        "id: widgets\nname: Widgets\nrepository: https://github.com/acme/widgets\nrequires_pip:\n"
        f"  - \"{NOPE}>=1\"\n  - \"pywinpty>=2.0; sys_platform == 'win32'\"\n",
        encoding="utf-8",
    )
    m = load_manifest(d)
    _wire(monkeypatch, enabled=[], disabled=[], meta=[{"id": "widgets", "enabled": True}])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "widgets"})
    monkeypatch.setattr(installer, "effective_copies", lambda: {"widgets": m})
    monkeypatch.setattr(installer, "effective_source_url", lambda pid: "https://github.com/acme/widgets.git")
    body = _client().post("/api/plugins/install", json={"url": "https://github.com/acme/widgets.git"}).json()
    [need] = body["deps_needed"]
    assert need["id"] == "widgets" and need["name"] == "Widgets"
    assert need["source"] == "https://github.com/acme/widgets.git"
    assert need["target"] == "this server's Python environment"
    # Only what applies HERE — the Windows-only pywinpty is not asked for on macOS.
    assert need["deps"] == [{"name": NOPE, "spec": f"{NOPE}>=1", "optional": False}]


def test_install_route_reports_no_deps_needed_for_the_terminal_plugin_on_macos(monkeypatch, tmp_path):
    from tests.test_plugin_routes import _client, _wire

    _on(monkeypatch, "darwin")
    m = _terminal(tmp_path)
    _wire(monkeypatch, enabled=[], disabled=[], meta=[{"id": "terminal", "enabled": True}])
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "terminal"})
    monkeypatch.setattr(installer, "effective_copies", lambda: {"terminal": m})
    body = _client().post("/api/plugins/install", json={"url": "https://github.com/protoLabsAI/terminal-plugin"}).json()
    assert body["deps_needed"] == []


def test_installed_route_does_not_list_a_marker_excluded_dep(monkeypatch, tmp_path):
    from tests.test_plugin_routes import _client, _wire

    _on(monkeypatch, "darwin")
    m = _terminal(tmp_path)
    _wire(monkeypatch, enabled=["terminal"], disabled=[], meta=[{"id": "terminal", "enabled": True}])
    monkeypatch.setattr(installer, "list_installed", lambda: [{"id": "terminal", "present": True}])
    monkeypatch.setattr(installer, "effective_copies", lambda: {"terminal": m})
    [row] = _client().get("/api/plugins/installed").json()["plugins"]
    assert row["deps_missing"] == []


def test_markers_use_this_process_environment_by_default():
    """No override: the marker is evaluated against the real interpreter."""
    import sys

    assert installer._dep_applies(f"{NOPE}; sys_platform == '{sys.platform}'")
    assert not installer._dep_applies(f"{NOPE}; sys_platform != '{sys.platform}'")


# ── the banner's install_deps action ────────────────────────────────────────────


def test_install_deps_action_is_forced_to_the_reporting_plugin():
    """A plugin can only ever offer to install ITS OWN declared deps: any target it
    supplies is replaced, and unknown extra keys are dropped."""
    from graph.plugins import setup_gaps

    setup_gaps.reset()
    setup_gaps.report(
        "mine", "k", "m", action={"kind": "install_deps", "target": "someone-else", "label": "Install", "cmd": "rm"}
    )
    assert setup_gaps.active()[0]["actions"] == [{"kind": "install_deps", "target": "mine", "label": "Install"}]
    setup_gaps.reset()


def test_cli_install_prints_only_the_deps_that_apply_here(monkeypatch, capsys, tmp_path):
    from graph.plugins import cli

    _on(monkeypatch, "darwin")
    summary = {
        "id": "terminal", "name": "Terminal", "version": "0.4.1", "description": "", "resolved_sha": "a" * 40,
        "repository": "", "requires_pip": ["pywinpty>=2.0; sys_platform == 'win32'"], "optional_pip": [],
        "contributes": {"views": []}, "capabilities": {},
    }
    monkeypatch.setattr(installer, "install", lambda *a, **k: summary)
    monkeypatch.setattr(installer, "configured_allowlist", lambda: None)
    assert cli.run_plugin_cli(["install", "https://github.com/protoLabsAI/terminal-plugin"]) == 0
    out = capsys.readouterr().out
    assert "pywinpty" not in out
