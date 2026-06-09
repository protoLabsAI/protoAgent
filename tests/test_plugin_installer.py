"""Git-URL plugin installer (ADR 0027) — fetch ≠ enable ≠ trust."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph.plugins import installer


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def _make_plugin_repo(root: Path, pid: str = "demo_ext", manifest_extra: str = "", tag: str | None = None) -> Path:
    repo = root / f"src-{pid}"
    repo.mkdir(parents=True)
    (repo / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: Demo Ext\nversion: 0.1.0\ndescription: a test plugin\n{manifest_extra}"
    )
    (repo / "__init__.py").write_text("def register(registry):\n    pass\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    if tag:
        _git(repo, "tag", tag)
    return repo


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Point the installer's lock + install dir + config dir at a temp area (never
    the real repo)."""
    monkeypatch.setattr(installer, "LOCK_PATH", tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "installed"))
    (tmp_path / "cfg").mkdir()
    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path


def test_install_fetches_code_writes_lock_does_not_enable(env):
    repo = _make_plugin_repo(env)
    summary = installer.install(str(repo))

    assert summary["id"] == "demo_ext"
    assert len(summary["resolved_sha"]) == 40
    # code landed in the live plugins dir, git metadata stripped
    target = installer.live_plugins_dir() / "demo_ext"
    assert (target / "protoagent.plugin.yaml").exists()
    assert not (target / ".git").exists()
    # lock recorded with provenance
    locked = installer.list_installed()
    assert locked[0]["id"] == "demo_ext" and locked[0]["present"] is True
    assert locked[0]["resolved_sha"] == summary["resolved_sha"]
    # install ≠ enable: nothing enabled it (no config touched, no register run)


def test_install_pins_a_tag(env):
    repo = _make_plugin_repo(env, tag="v1")
    summary = installer.install(str(repo), "v1")
    assert summary["requested_ref"] == "v1" and len(summary["resolved_sha"]) == 40


def test_duplicate_requires_force(env):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    with pytest.raises(installer.InstallError, match="already installed"):
        installer.install(str(repo))
    installer.install(str(repo), force=True)  # ok with force


def test_refuses_to_shadow_a_builtin(env):
    # `hello` is a real built-in plugin in the repo — must not be installable over.
    repo = _make_plugin_repo(env, pid="hello")
    with pytest.raises(installer.InstallError, match="built-in"):
        installer.install(str(repo))


def test_repo_without_manifest_is_rejected(env, tmp_path):
    bare = tmp_path / "src-bare"
    bare.mkdir()
    (bare / "README.md").write_text("not a plugin")
    _git(bare, "init", "-q")
    _git(bare, "add", "-A")
    _git(bare, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    with pytest.raises(installer.InstallError, match="not a protoAgent plugin"):
        installer.install(str(bare))


def test_bad_url_scheme_rejected(env):
    with pytest.raises(installer.InstallError, match="unsupported source"):
        installer.install("ftp://evil.example/x.git")


def test_uninstall_removes_code_and_lock(env):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    installer.uninstall("demo_ext")
    assert not (installer.live_plugins_dir() / "demo_ext").exists()
    assert installer.list_installed() == []
    with pytest.raises(installer.InstallError, match="not installed"):
        installer.uninstall("demo_ext")


def test_sync_recolones_missing_from_lock(env):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    # simulate a fresh checkout: code gone, lock present
    import shutil
    shutil.rmtree(installer.live_plugins_dir() / "demo_ext")
    assert installer.list_installed()[0]["present"] is False
    results = installer.sync()
    assert results == [{"id": "demo_ext", "status": "installed"}]
    assert (installer.live_plugins_dir() / "demo_ext").exists()


def test_source_allowlist_blocks_offlist(env):
    repo = _make_plugin_repo(env)
    with pytest.raises(installer.InstallError, match="not on plugins.sources.allow"):
        installer.install(str(repo), allow=["github.com/protoLabsAI/*"])


def test_install_deps_noop_without_deps(env):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    assert installer.install_deps("demo_ext") == []


def test_install_deps_missing_plugin(env):
    with pytest.raises(installer.InstallError, match="not installed"):
        installer.install_deps("nope")


def test_install_deps_runs_pip_with_declared_deps(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [requests>=2, rich]\n")
    installer.install(str(repo))
    calls = []

    class _OK:
        returncode = 0
        stderr = ""
        stdout = ""

    def _fake_run(cmd, **kw):
        calls.append(cmd)
        return _OK()

    monkeypatch.setattr(installer.subprocess, "run", _fake_run)  # don't hit the network
    deps = installer.install_deps("demo_ext")
    assert deps == ["requests>=2", "rich"]
    assert calls and calls[0][1:4] == ["-m", "pip", "install"]
    assert calls[0][4:] == ["requests>=2", "rich"]


def test_uninstall_removes_enabled_ref_keeps_config(env):
    cfg = env / "cfg" / "langgraph-config.yaml"
    cfg.write_text(
        "plugins:\n  enabled: [demo_ext, other]\n"
        "demo_ext:\n  greeting: hi\n"
    )
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    rep = installer.uninstall("demo_ext")  # no purge
    assert "enabled-ref" in rep["removed"]
    text = cfg.read_text()
    assert "demo_ext" not in _enabled_list(text)   # dropped from plugins.enabled
    assert "other" in _enabled_list(text)          # siblings untouched
    assert "demo_ext:" in text                      # config section KEPT (no purge)


def test_uninstall_purge_removes_config_and_secrets(env):
    cfg = env / "cfg" / "langgraph-config.yaml"
    cfg.write_text("plugins:\n  enabled: [demo_ext]\ndemo_ext:\n  greeting: hi\n")
    secrets = env / "cfg" / "secrets.yaml"
    secrets.write_text("demo_ext:\n  api_key: SEKRET\nmodel:\n  api_key: keep\n")
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    rep = installer.uninstall("demo_ext", purge=True)
    assert set(rep["removed"]) >= {"code", "config", "secrets"}
    assert "demo_ext" not in cfg.read_text()        # section + enabled ref gone
    assert "demo_ext" not in secrets.read_text()     # secrets gone
    assert "model" in secrets.read_text()            # other secrets kept


def _enabled_list(yaml_text: str) -> str:
    import yaml as _y
    return str((_y.safe_load(yaml_text).get("plugins") or {}).get("enabled") or [])


def test_configured_allowlist_reads_config(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "langgraph-config.yaml").write_text(
        "plugins:\n  sources:\n    allow: [github.com/protoLabsAI/*]\n"
    )
    monkeypatch.setenv("PROTOAGENT_CONFIG_DIR", str(cfg_dir))
    assert installer.configured_allowlist() == ["github.com/protoLabsAI/*"]


# ── bundles (a repo of plugin references, installed together) ─────────────────
def _make_bundle_repo(root: Path, members: list[Path]) -> Path:
    """A bundle repo: protoagent.bundle.yaml referencing member plugin repos by
    local path, plus a builtin entry that must be skipped."""
    repo = root / "src-bundle"
    repo.mkdir(parents=True)
    lines = ["id: demo_stack", "name: Demo Stack", "description: a test bundle", "plugins:"]
    lines.append("  - { id: delegates, builtin: true }")
    for m in members:
        # id is read from each member's manifest on install; the bundle only needs url
        lines.append(f"  - {{ id: x, url: {m} }}")
    lines += ["enabled: [delegates, demo_a, demo_b]", "config:", "  demo_a: { k: v }"]
    (repo / "protoagent.bundle.yaml").write_text("\n".join(lines) + "\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return repo


def test_install_bundle_fans_out_and_records_provenance(env):
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])

    summary = installer.install(str(bundle))

    # returns a bundle summary, installs both members, skips the builtin
    assert summary["bundle"] == "demo_stack"
    assert {p["id"] for p in summary["installed"]} == {"demo_a", "demo_b"}
    assert summary["skipped_builtin"] == ["delegates"]
    # enable list + config are surfaced (suggested), not applied
    assert summary["enabled"] == ["delegates", "demo_a", "demo_b"]
    assert summary["config"] == {"demo_a": {"k": "v"}}

    # both members landed + are pinned individually; the bundle is recorded
    assert (installer.live_plugins_dir() / "demo_a" / "protoagent.plugin.yaml").exists()
    assert (installer.live_plugins_dir() / "demo_b" / "protoagent.plugin.yaml").exists()
    lock = installer._read_lock()
    assert {e["id"] for e in lock["plugins"]} >= {"demo_a", "demo_b"}
    assert any(e["by"] == "bundle:demo_stack" for e in lock["plugins"])
    bundles = lock.get("bundles") or []
    assert bundles and bundles[0]["id"] == "demo_stack"
    assert set(bundles[0]["plugins"]) == {"demo_a", "demo_b"}


def test_install_bundle_member_missing_url_errors(env):
    repo = env / "src-badbundle"
    repo.mkdir(parents=True)
    (repo / "protoagent.bundle.yaml").write_text(
        "id: bad\nplugins:\n  - { id: nope }\n"  # no url, not builtin
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    with pytest.raises(installer.InstallError):
        installer.install(str(repo))
