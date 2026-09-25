"""The desktop app gets the SAME install-time consent dialog as the browser console.

#3618 gave the console an "Install Python packages for <Plugin>?" dialog driven by the
install response's ``deps_needed``. On the frozen desktop app, though, ``install()`` pip'd a
plugin's missing required deps into the managed Python runtime by itself (#2226), so the
dialog almost never had anything to ask. Now a frozen install lands the plugin and REPORTS
the deps like a server install; the operator's confirm runs the existing install-deps route,
which targets the managed runtime.

These run the real route → op → installer → ``runtime.python_install`` chain in frozen mode
(``PROTOAGENT_PLUGIN_FROZEN=1``, the same switch the installer's own tests use) against a
fake managed runtime laid out at its REAL box-root path. The only thing faked is the
process boundary: ``subprocess.run`` inside ``runtime.python_install`` — the pip that would
run in the managed interpreter — which records its argv and "installs" by writing the
``.dist-info`` a real pip would, so the post-install dep check reads the real layout.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from graph.plugins import installer
from tests.test_plugin_installer import _git
from tests.test_plugin_routes import _client, _wire

PKG = "leftpad-desktop-xyz"  # never importable in the host: a real "missing" dep


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    """A frozen desktop instance: temp plugins dir + lock + config, a temp box root with a
    provisioned (fake) managed runtime, and pip stubbed at the subprocess boundary."""
    import graph.config_io as cio
    import infra.python_runtime as pr
    import runtime.python_install as pi
    from infra.paths import reset_instance_paths

    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "installed"))
    (tmp_path / "cfg").mkdir()
    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "cfg" / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "cfg" / "secrets.yaml")
    monkeypatch.setenv("PROTOAGENT_BOX_ROOT", str(tmp_path / "box"))
    reset_instance_paths()
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FETCH", "git")  # frozen prefers archives; clone the local repo
    monkeypatch.setattr(installer, "_audit", lambda *a, **k: None)

    # The provisioned runtime, at the path the real resolver computes.
    install_dir = pr.managed_python_install_dir()
    exe = pr._python_exe_in(install_dir)
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    site = install_dir / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)

    calls: list[list[str]] = []
    real_run = subprocess.run

    def _fake_run(argv, *a, **kw):
        # `pi.subprocess` IS the global module, so only the managed interpreter's runs are
        # faked; everything else (the installer's own git clone) runs for real.
        if not argv or str(argv[0]) != str(exe):
            return real_run(argv, *a, **kw)
        argv = [str(x) for x in argv]
        calls.append(argv)
        if argv[1:4] == ["-m", "pip", "install"]:
            for spec in argv[argv.index("--") + 1 :]:
                name = installer._dep_pkg_name(spec).replace("-", "_")
                (site / f"{name}-1.0.dist-info").mkdir()
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(pi.subprocess, "run", _fake_run)
    yield {"root": tmp_path, "exe": exe, "site": site, "pip": calls}
    reset_instance_paths()


def _plugin_repo(root: Path, requires: str) -> Path:
    repo = root / "src-depdemo"
    repo.mkdir()
    (repo / "protoagent.plugin.yaml").write_text(
        f"id: depdemo\nname: Dep Demo\nversion: 0.1.0\nrepository: https://github.com/acme/depdemo\n{requires}",
        encoding="utf-8",
    )
    (repo / "__init__.py").write_text("def register(registry):\n    pass\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def _pip_installs(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[1:4] == ["-m", "pip", "install"]]


def test_frozen_install_reports_deps_then_the_confirm_installs_them_into_the_managed_runtime(desktop, monkeypatch):
    repo = _plugin_repo(desktop["root"], f"requires_pip:\n  - \"{PKG}>=1\"\n  - {{pkg: 'softpad-xyz>=2', optional: true}}\n")
    _wire(monkeypatch, enabled=[], disabled=[], meta=[{"id": "depdemo", "enabled": True, "loaded": True}])

    # 1) Install: the plugin lands, NOTHING is pip'd, and the response carries what the
    #    console's dialog lists — exact specs, the source, and the desktop runtime target.
    body = _client().post("/api/plugins/install", json={"url": str(repo)}).json()
    assert body["installed"]["id"] == "depdemo", body
    assert (installer.live_plugins_dir() / "depdemo" / "protoagent.plugin.yaml").exists()
    assert desktop["pip"] == []  # no silent pip at install time
    [need] = body["deps_needed"]
    assert need["id"] == "depdemo" and need["name"] == "Dep Demo"
    assert need["source"] == str(repo)
    assert need["target"] == "the desktop app's managed Python runtime"
    assert need["deps"] == [
        {"name": PKG, "spec": f"{PKG}>=1", "optional": False},
        {"name": "softpad-xyz", "spec": "softpad-xyz>=2", "optional": True},
    ]

    # 2) Confirm: the dialog posts the existing install-deps route, which pips into the
    #    MANAGED runtime's interpreter (never the frozen host), hard + optional together.
    res = _client().post("/api/plugins/install-deps", json={"id": "depdemo"})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["ok"] is True
    assert out["installed"] == [f"{PKG}>=1", "softpad-xyz>=2"]
    [pip] = _pip_installs(desktop["pip"])
    assert pip[0] == str(desktop["exe"])  # the managed interpreter, not sys.executable
    assert pip[0] != sys.executable
    assert pip[pip.index("--") + 1 :] == [f"{PKG}>=1", "softpad-xyz>=2"]

    # 3) The gap closes: the dep check now finds them in the runtime's site-packages.
    m = installer.effective_copies()["depdemo"]
    assert installer.missing_deps_detail(m) == []


def test_frozen_install_with_nothing_missing_asks_nothing(desktop, monkeypatch):
    # httpx ships in the host (a core dep) — nothing for the dialog to ask.
    repo = _plugin_repo(desktop["root"], "requires_pip: [\"httpx>=0.27\"]\n")
    _wire(monkeypatch, enabled=[], disabled=[], meta=[])
    body = _client().post("/api/plugins/install", json={"url": str(repo)}).json()
    assert body["deps_needed"] == []
    assert desktop["pip"] == []


def test_frozen_install_route_still_refuses_a_hard_host_scoped_dep(desktop, monkeypatch):
    """Unchanged (#2246, the prototrader-finance case): no confirm can satisfy a HOST-scoped
    import from the managed runtime, so the install itself is refused — no code lands, no pip."""
    repo = _plugin_repo(desktop["root"], f"requires_pip:\n  - {{pkg: '{PKG}>=1', scope: host}}\n")
    _wire(monkeypatch, enabled=[], disabled=[], meta=[])
    res = _client().post("/api/plugins/install", json={"url": str(repo)})
    assert res.status_code == 400
    assert "HOST-scoped dep, which a frozen app cannot satisfy" in res.json()["detail"]
    assert not (installer.live_plugins_dir() / "depdemo").exists()
    assert desktop["pip"] == []


def test_cli_install_runtime_deps_flag_keeps_non_interactive_provisioning_working(desktop, capsys):
    """The fleet's archetype create and snapshot import spawn `plugin install … --install-runtime-deps`:
    they still pip missing required deps into the managed runtime as part of the install."""
    from graph.plugins.cli import run_plugin_cli

    repo = _plugin_repo(desktop["root"], f"requires_pip: [\"{PKG}>=1\"]\n")
    assert run_plugin_cli(["install", str(repo), "--install-runtime-deps"]) == 0
    [pip] = _pip_installs(desktop["pip"])
    assert pip[0] == str(desktop["exe"]) and pip[-1] == f"{PKG}>=1"
    assert "installed into the managed Python runtime" in capsys.readouterr().out


def test_cli_install_without_the_flag_leaves_deps_for_install_deps(desktop, capsys):
    from graph.plugins.cli import run_plugin_cli

    repo = _plugin_repo(desktop["root"], f"requires_pip: [\"{PKG}>=1\"]\n")
    assert run_plugin_cli(["install", str(repo)]) == 0
    assert desktop["pip"] == []
    out = capsys.readouterr().out
    assert "NOT installed" in out and "protoagent plugin install-deps depdemo" in out


def test_non_interactive_provisioning_spawns_the_cli_with_the_opt_in(monkeypatch, tmp_path):
    """Both unattended install paths pass the explicit opt-in to the CLI they spawn."""
    import graph.snapshot_import as si
    import graph.workspaces.manager as mgr

    seen: list[list[str]] = []

    def _run(argv, **kw):
        seen.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    mgr._install_bundle_into(tmp_path, "https://github.com/acme/stack")
    pin = type("Pin", (), {"id": "p", "url": "https://github.com/acme/p", "ref": "v1"})()
    monkeypatch.setattr(mgr, "_enable_installed_in_config", lambda *a, **k: None)
    si._install_pins(tmp_path, [pin])
    installs = [a for a in seen if "install" in a]
    assert len(installs) == 2  # the fleet create + the snapshot import
    assert all("--install-runtime-deps" in argv for argv in installs)
