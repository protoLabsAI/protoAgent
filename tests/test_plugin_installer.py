"""Git-URL plugin installer (ADR 0027) — fetch ≠ enable ≠ trust."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from graph.plugins import installer


def _git(cwd: Path, *args: str) -> None:
    # maintenance.auto=false / gc.auto=0: `git commit` spawns a DETACHED
    # `git maintenance run --auto` in the fixture repo, whose pack-file churn can
    # race a subsequent clone of that repo (#1600). Fixture repos stay inert.
    subprocess.run(
        ["git", "-c", "maintenance.auto=false", "-c", "gc.auto=0", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


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
    """Point the installer's lock + install dir + config/secrets at a temp area
    (never the real repo)."""
    import graph.config_io as cio

    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "installed"))
    (tmp_path / "cfg").mkdir()
    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "cfg" / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "cfg" / "secrets.yaml")
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


def test_reinstall_same_source_same_commit_is_up_to_date(env):
    """Re-install from the plugin's own origin at the same commit = converge, not
    conflict — a bundle re-install must not die on already-installed members."""
    repo = _make_plugin_repo(env)
    first = installer.install(str(repo))
    again = installer.install(str(repo))  # no force needed
    assert again["up_to_date"] is True
    assert again["resolved_sha"] == first["resolved_sha"]
    # lock unchanged — one entry, original provenance kept
    lock = installer._read_lock()
    assert [e["id"] for e in lock["plugins"]] == ["demo_ext"]


def test_reinstall_same_source_new_commit_updates_without_force(env):
    repo = _make_plugin_repo(env)
    first = installer.install(str(repo))
    (repo / "extra.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "update")
    updated = installer.install(str(repo))  # a moved ref from the same origin: update
    assert updated["resolved_sha"] != first["resolved_sha"]
    assert not updated.get("up_to_date")
    assert (installer.live_plugins_dir() / "demo_ext" / "extra.py").exists()
    lock = installer._read_lock()
    assert [e["resolved_sha"] for e in lock["plugins"] if e["id"] == "demo_ext"] == [updated["resolved_sha"]]


def test_same_id_from_different_source_requires_force(env):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    other = _make_plugin_repo(env / "elsewhere", pid="demo_ext")  # same id, different origin
    with pytest.raises(installer.InstallError, match="different source|already installed"):
        installer.install(str(other))
    installer.install(str(other), force=True)  # explicit clobber still works


def test_untracked_dir_requires_force(env):
    """A dir the lock doesn't know (working-tree / hand-copied plugin) must not be
    silently clobbered by a git install of the same id."""
    repo = _make_plugin_repo(env)
    tree = installer.live_plugins_dir() / "demo_ext"
    tree.mkdir(parents=True)
    (tree / "protoagent.plugin.yaml").write_text("id: demo_ext\nname: local\nversion: 0.0.1\ndescription: wip\n")
    with pytest.raises(installer.InstallError, match="untracked"):
        installer.install(str(repo))
    installer.install(str(repo), force=True)


def test_refuses_to_shadow_a_builtin(env):
    # `hello` is a real built-in plugin in the repo — must not be installable over.
    repo = _make_plugin_repo(env, pid="hello")
    with pytest.raises(installer.InstallError, match="built-in"):
        installer.install(str(repo))


def test_ghost_dir_without_manifest_does_not_block_install(env, tmp_path, monkeypatch):
    # A manifest-less leftover under plugins/<id> (e.g. a __pycache__ dir orphaned
    # when a plugin was extracted core→standalone, #1731) is NOT a built-in and
    # must not block installing the standalone successor of the same id.
    builtins = tmp_path / "builtins"
    ghost = builtins / "ghost_ext" / "__pycache__"
    ghost.mkdir(parents=True)
    (ghost / "loader.cpython-312.pyc").write_bytes(b"\x00")
    monkeypatch.setattr(installer, "bundled_plugins_dir", lambda: builtins)

    repo = _make_plugin_repo(env, pid="ghost_ext")
    summary = installer.install(str(repo))  # must not raise "is a built-in"
    assert summary["id"] == "ghost_ext"


def test_manifest_dir_is_treated_as_builtin(env, tmp_path, monkeypatch):
    # A directory that DOES hold a manifest is a real built-in and still blocks.
    builtins = tmp_path / "builtins"
    real = builtins / "real_ext"
    real.mkdir(parents=True)
    (real / "protoagent.plugin.yaml").write_text("id: real_ext\nname: R\nversion: 0.1.0\ndescription: x\n")
    monkeypatch.setattr(installer, "bundled_plugins_dir", lambda: builtins)

    repo = _make_plugin_repo(env, pid="real_ext")
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


def test_list_installed_surfaces_untracked_local_copy(env):
    # Disk is the source of truth: a plugin dir hand-placed into the live plugins
    # dir (a gitignored local/dev copy) is NOT in plugins.lock, but it loads — so it
    # must still be listed, marked tracked:False, not hidden.
    installed = _make_plugin_repo(env)
    installer.install(str(installed))  # tracked: on disk + in the lock

    local = installer.live_plugins_dir() / "local_ext"
    local.mkdir(parents=True)
    (local / "protoagent.plugin.yaml").write_text(
        "id: local_ext\nname: Local Ext\nversion: 0.1.0\ndescription: a local copy\n"
    )

    rows = {r["id"]: r for r in installer.list_installed()}
    assert set(rows) == {"demo_ext", "local_ext"}
    assert rows["demo_ext"]["tracked"] is True and rows["demo_ext"]["present"] is True
    assert rows["local_ext"]["tracked"] is False and rows["local_ext"]["present"] is True
    assert rows["local_ext"]["source_url"] == "" and rows["local_ext"]["resolved_sha"] == ""


def test_list_installed_ignores_non_plugin_dirs(env):
    # A stray dir without a manifest isn't a plugin (mirror the loader) — not listed.
    stray = installer.live_plugins_dir() / "not_a_plugin"
    stray.mkdir(parents=True)
    (stray / "README.md").write_text("nope")
    assert installer.list_installed() == []


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
    # `--` ends pip option parsing so a manifest dep can't be read as a flag.
    assert calls[0][4:] == ["--", "requests>=2", "rich"]


@pytest.mark.parametrize(
    "bad",
    [
        "--index-url=https://evil.example/simple",
        "-e .",
        "git+https://evil.example/pkg.git",
        "foo @ https://evil.example/foo.whl",
        "https://evil.example/foo.tar.gz",
    ],
)
def test_install_deps_rejects_non_pep508_requires_pip(env, bad):
    """A plugin manifest can't smuggle pip options / VCS+URL refs through requires_pip."""
    repo = _make_plugin_repo(env, manifest_extra=f"requires_pip: ['{bad}']\n")
    installer.install(str(repo))
    with pytest.raises(installer.InstallError):
        installer.install_deps("demo_ext")


@pytest.mark.parametrize("bad", ["--index-url=https://evil.example/simple", "git+https://evil.example/pkg.git"])
def test_install_deps_rejects_bad_optional_specs(env, bad):
    """The _validate_pip_specs rails cover the optional tier too (#1953)."""
    repo = _make_plugin_repo(env, manifest_extra=f"requires_pip: [{{pkg: '{bad}', optional: true}}]\n")
    installer.install(str(repo))
    with pytest.raises(installer.InstallError):
        installer.install_deps("demo_ext")


# ── optional dep tier (#1953) — install-deps installs optional deps best-effort ──


class _PipResult:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stderr = "boom" if returncode else ""
        self.stdout = ""


def test_install_deps_includes_optional_in_own_pip_call(env, monkeypatch):
    repo = _make_plugin_repo(
        env,
        manifest_extra="requires_pip: [requests>=2, {pkg: 'pillow>=10', optional: true}]\n",
    )
    installer.install(str(repo))
    calls = []
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or _PipResult())
    deps = installer.install_deps("demo_ext")
    assert deps == ["requests>=2", "pillow>=10"]
    # hard deps first (fail-hard), then the optional tier best-effort — both behind `--`
    assert [c[4:] for c in calls] == [["--", "requests>=2"], ["--", "pillow>=10"]]


def test_install_deps_optional_pip_failure_warns_not_fails(env, monkeypatch, caplog):
    """A failed optional install must not fail the command — the hard deps landed."""
    import logging as _logging

    repo = _make_plugin_repo(
        env,
        manifest_extra="requires_pip: [requests>=2, {pkg: 'pillow>=10', optional: true}]\n",
    )
    installer.install(str(repo))
    # hard pip call succeeds; the optional one fails
    monkeypatch.setattr(
        installer.subprocess, "run", lambda cmd, **kw: _PipResult(returncode=1 if "pillow>=10" in cmd else 0)
    )
    with caplog.at_level(_logging.WARNING):
        deps = installer.install_deps("demo_ext")  # no raise
    assert deps == ["requests>=2"]  # only what actually installed
    assert "optional dep install failed" in caplog.text


def test_install_deps_only_optional_failure_still_succeeds(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [{pkg: 'pillow>=10', optional: true}]\n")
    installer.install(str(repo))
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: _PipResult(returncode=1))
    assert installer.install_deps("demo_ext") == []  # warned, not raised


def test_install_deps_hard_pip_failure_still_raises(env, monkeypatch):
    """Hard-dep failure keeps today's behavior even when an optional tier exists."""
    repo = _make_plugin_repo(
        env,
        manifest_extra="requires_pip: [requests>=2, {pkg: 'pillow>=10', optional: true}]\n",
    )
    installer.install(str(repo))
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: _PipResult(returncode=1))
    with pytest.raises(installer.InstallError, match="pip install failed"):
        installer.install_deps("demo_ext")


def test_uninstall_removes_enabled_ref_keeps_config(env):
    cfg = env / "cfg" / "langgraph-config.yaml"
    cfg.write_text("plugins:\n  enabled: [demo_ext, other]\ndemo_ext:\n  greeting: hi\n")
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    rep = installer.uninstall("demo_ext")  # no purge
    assert "enabled-ref" in rep["removed"]
    text = cfg.read_text()
    assert "demo_ext" not in _enabled_list(text)  # dropped from plugins.enabled
    assert "other" in _enabled_list(text)  # siblings untouched
    assert "demo_ext:" in text  # config section KEPT (no purge)


def test_uninstall_purge_removes_config_and_secrets(env):
    cfg = env / "cfg" / "langgraph-config.yaml"
    cfg.write_text("plugins:\n  enabled: [demo_ext]\ndemo_ext:\n  greeting: hi\n")
    secrets = env / "cfg" / "secrets.yaml"
    secrets.write_text("demo_ext:\n  api_key: SEKRET\nmodel:\n  api_key: keep\n")
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    rep = installer.uninstall("demo_ext", purge=True)
    assert set(rep["removed"]) >= {"code", "config", "secrets"}
    assert "demo_ext" not in cfg.read_text()  # section + enabled ref gone
    assert "demo_ext" not in secrets.read_text()  # secrets gone
    assert "model" in secrets.read_text()  # other secrets kept


def _enabled_list(yaml_text: str) -> str:
    import yaml as _y

    return str((_y.safe_load(yaml_text).get("plugins") or {}).get("enabled") or [])


def test_configured_allowlist_reads_config(tmp_path, monkeypatch):
    import graph.config_io as cio

    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    (cfg_dir / "langgraph-config.yaml").write_text("plugins:\n  sources:\n    allow: [github.com/protoLabsAI/*]\n")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: cfg_dir / "langgraph-config.yaml")
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
    # the curated turn-on list is persisted in the lock (#1346) so a lock-only consumer
    # (the fleet new-agent path) can auto-enable exactly what the author intended.
    assert bundles[0]["enabled"] == ["delegates", "demo_a", "demo_b"]
    # ...and the recommended config defaults too (#1350), for the same consumer.
    assert bundles[0]["config"] == {"demo_a": {"k": "v"}}


def test_bundle_reinstall_converges_instead_of_erroring(env):
    """Re-running a bundle install over an already-provisioned host must converge:
    unchanged members report up-to-date, a member whose repo moved gets updated —
    never an 'already installed' abort mid-fan-out."""
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))

    # advance one member's repo (a moved pin)
    (a / "extra.py").write_text("x = 1\n")
    _git(a, "add", "-A")
    _git(a, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "update")

    summary = installer.install(str(bundle))  # no force
    by_id = {p["id"]: p for p in summary["installed"]}
    assert not by_id["demo_a"].get("up_to_date")  # updated to the new commit
    assert by_id["demo_b"].get("up_to_date") is True  # unchanged — converged
    assert (installer.live_plugins_dir() / "demo_a" / "extra.py").exists()


def test_bundle_config_overlay_fills_only_unset_keys():
    """Defaults overlay: a key the operator already set is left untouched; only the
    unset keys are filled, and a fully-set section is dropped (#1350)."""
    bundle_config = {
        "agent_browser": {"panel_mode": "full", "timeout": 30},
        "board": {"theme": "dark"},
    }
    current = {
        "agent_browser": {"panel_mode": "compact"},  # operator already chose this — keep it
        "board": {"theme": "light"},  # fully set → section dropped
    }
    overlay = installer.bundle_config_overlay(bundle_config, current)
    assert overlay == {"agent_browser": {"timeout": 30}}  # only the unset key; operator value wins


def test_bundle_config_overlay_empty_and_malformed():
    assert installer.bundle_config_overlay(None, {}) == {}
    assert installer.bundle_config_overlay({}, None) == {}
    # a non-dict section value is skipped, not crashed on
    assert installer.bundle_config_overlay({"x": "notadict"}, {}) == {}
    # no current → every key is unset, so all fill
    assert installer.bundle_config_overlay({"x": {"a": 1}}, {}) == {"x": {"a": 1}}


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


# ── private-repo clone auth (github HTTPS token) ──────────────────────────────
# A runtime `plugin install` of a PRIVATE github repo used to fail on the default git
# path ("could not read Username for 'https://github.com'") — git got no credential in a
# container with only a token env. `_git_auth_env` hands git a scoped http.extraheader via
# GIT_CONFIG_* so the private clone authenticates, off-argv and off-.git/config.


def test_git_auth_env_github_https_with_token(monkeypatch):
    import base64

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "tok")
    e = installer._git_auth_env("https://github.com/org/repo")
    assert e["GIT_CONFIG_COUNT"] == "1"
    assert e["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    scheme, b64 = e["GIT_CONFIG_VALUE_0"].removeprefix("Authorization: ").split()
    assert scheme == "Basic"
    assert base64.b64decode(b64).decode() == "x-access-token:tok"


def test_git_auth_env_prefers_github_token_over_gh_token(monkeypatch):
    import base64

    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    monkeypatch.setenv("GH_TOKEN", "alt")
    e = installer._git_auth_env("https://github.com/o/r")
    assert base64.b64decode(e["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:gh"


def test_git_auth_env_empty_for_ssh_nongithub_or_no_token(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "tok")
    assert installer._git_auth_env("git@github.com:o/r.git") == {}       # ssh → git's own auth
    assert installer._git_auth_env("https://gitlab.com/o/r") == {}       # non-github
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert installer._git_auth_env("https://github.com/o/r") == {}       # no token


def test_clone_authenticates_private_github_via_scoped_env(monkeypatch, tmp_path):
    """_clone passes the auth env to the network `clone` (not to local checkout/rev-parse),
    the token never appears in argv, and the header is scoped to github.com HTTPS."""
    import base64

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "s3cr3t")
    calls = []

    def fake_git(*args, cwd=None, timeout=None, env=None):
        calls.append({"args": args, "env": env})
        return "b" * 40  # stands in for `rev-parse HEAD`

    monkeypatch.setattr(installer, "_git", fake_git)
    installer._clone("https://github.com/protoLabsAI/frontend-bundle", None, tmp_path / "dest")

    clone = next(c for c in calls if c["args"][0] == "clone")
    env = clone["env"]
    assert env and env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:s3cr3t"
    assert not any("s3cr3t" in str(a) for a in clone["args"])  # off-argv: no `ps` leak
    rev = next(c for c in calls if c["args"][0] == "rev-parse")
    assert rev["env"] is None  # local op inherits os.environ, no auth injected


def test_clone_no_auth_env_without_token(monkeypatch, tmp_path):
    """No token → clone runs with env=None (git's own auth), unchanged behavior."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = []
    monkeypatch.setattr(installer, "_git", lambda *a, cwd=None, timeout=None, env=None: calls.append({"args": a, "env": env}) or "b" * 40)
    installer._clone("https://github.com/o/r", None, tmp_path / "dest")
    clone = next(c for c in calls if c["args"][0] == "clone")
    assert clone["env"] is None


# ── private-repo auth for the update CHECK (ls-remote), not just install (#1805 follow-up) ──
# check_updates ls-remotes each plugin's repo; for a PRIVATE repo the plain ls-remote failed
# auth → "check failed" in the panel even though the plugin works. Same _git_auth_env, applied
# to the update-check path.


def test_ls_remote_sha_authenticates_private_github(monkeypatch):
    import base64

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "s3cr3t")
    cap = {}

    def fake_git(*args, cwd=None, timeout=None, env=None):
        cap["args"], cap["env"] = args, env
        return "abc1230000000000000000000000000000000000\trefs/heads/main"

    monkeypatch.setattr(installer, "_git", fake_git)
    installer._lsremote_cache.clear()
    installer._ls_remote_sha("https://github.com/protoLabsAI/private-plugin", "main")

    assert cap["args"][0] == "ls-remote"
    env = cap["env"]
    assert env and env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert base64.b64decode(env["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:s3cr3t"


def test_ls_remote_tags_authenticates_private_github(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "tok")
    cap = {}
    monkeypatch.setattr(installer, "_git", lambda *a, cwd=None, timeout=None, env=None: (cap.update(args=a, env=env) or "sha\trefs/tags/v0.1.0"))
    installer._lstags_cache.clear()
    installer._ls_remote_tags("https://github.com/protoLabsAI/private-plugin")
    assert cap["env"] and cap["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_ls_remote_no_auth_env_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    cap = {}
    monkeypatch.setattr(installer, "_git", lambda *a, cwd=None, timeout=None, env=None: (cap.update(env=env) or "sha\tHEAD"))
    installer._lsremote_cache.clear()
    installer._ls_remote_sha("https://github.com/protoLabsAI/public-plugin", "")
    assert cap["env"] is None  # no token → plain env, unchanged behavior
