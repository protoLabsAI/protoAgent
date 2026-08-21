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
    deps = [{"name": "demolib", "version": "1.0.0", "sha256": "a" * 64}]
    lock = installer._read_lock()
    next(e for e in lock["plugins"] if e["id"] == "demo_ext")["deps"] = deps
    installer._write_lock(lock)
    (repo / "extra.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "update")
    updated = installer.install(str(repo))  # a moved ref from the same origin: update
    assert updated["resolved_sha"] != first["resolved_sha"]
    assert not updated.get("up_to_date")
    assert (installer.live_plugins_dir() / "demo_ext" / "extra.py").exists()
    lock = installer._read_lock()
    assert [e["resolved_sha"] for e in lock["plugins"] if e["id"] == "demo_ext"] == [updated["resolved_sha"]]
    assert next(e for e in lock["plugins"] if e["id"] == "demo_ext")["deps"] == deps


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


# ── deps-time allowlist re-check (#2743): "was allowed then" ≠ "is allowed now" ──


def test_install_deps_refuses_when_source_left_the_allowlist(env, monkeypatch):
    # Installed while the allowlist was open; the operator then locks installs down.
    # The next deps install (pip = code-adjacent) must re-check against the CURRENT
    # allowlist, not the one that existed at install time.
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    monkeypatch.setattr(installer, "configured_allowlist", lambda: ["github.com/protoLabsAI/*"])
    with pytest.raises(installer.InstallError, match="no longer on"):
        installer.install_deps("demo_ext")


def test_install_deps_proceeds_when_source_still_allowed(env, monkeypatch):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    # An exact-source allow entry — the same match predicate install itself uses.
    monkeypatch.setattr(installer, "configured_allowlist", lambda: [str(repo)])
    assert installer.install_deps("demo_ext") == []  # no deps declared — reaches the noop path


def test_install_deps_skips_recheck_without_a_recorded_origin(env, monkeypatch):
    # A hand-copied working-tree plugin has no lock entry: nothing was fetched, so
    # there is no origin to re-validate — the allowlist must not brick it.
    import yaml as _y

    target = installer.live_plugins_dir() / "handmade"
    target.mkdir(parents=True)
    (target / "protoagent.plugin.yaml").write_text(
        _y.safe_dump({"id": "handmade", "name": "Handmade", "version": "0.1.0"})
    )
    monkeypatch.setattr(installer, "configured_allowlist", lambda: ["github.com/protoLabsAI/*"])
    assert installer.install_deps("handmade") == []


def test_recorded_source_url_reads_the_lock(env):
    repo = _make_plugin_repo(env)
    installer.install(str(repo))
    assert installer.recorded_source_url("demo_ext") == str(repo)
    assert installer.recorded_source_url("absent") == ""


def test_recorded_source_url_tolerates_a_malformed_lock_entry(env):
    # _normalize_lock preserves non-dict members of a hand-edited lock — the
    # reader must skip them, not AttributeError before install_deps even starts.
    import json

    installer.lock_path().write_text(
        json.dumps({"plugins": ["garbage-string", {"id": "real", "source_url": "https://github.com/x/y"}]})
    )
    assert installer.recorded_source_url("real") == "https://github.com/x/y"
    assert installer.recorded_source_url("other") == ""


def test_install_deps_missing_plugin(env):
    with pytest.raises(installer.InstallError, match="not installed"):
        installer.install_deps("nope")


# ── frozen desktop: deps route into the managed Python runtime (ADR 0094 P2) ──


def test_deps_satisfied_honors_the_managed_runtime_when_frozen(env, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    # A dep that's NOT importable in the host is still "satisfied" if it's in the runtime.
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: {"python-docx"})
    ok, missing = installer._deps_satisfied(["python-docx>=1.1", "nope-pkg"])
    assert not ok and missing == ["nope-pkg"]  # python-docx satisfied via the runtime


def test_frozen_install_deps_pips_into_managed_runtime(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [python-docx>=1.1]\n")
    installer.install(str(repo))
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())  # not yet in the runtime
    import runtime.python_install as pi

    got = {}
    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", lambda reqs, **k: got.setdefault("reqs", reqs))
    deps = installer.install_deps("demo_ext")
    assert got["reqs"] == ["python-docx>=1.1"]  # routed to the managed runtime, not host pip
    assert deps == ["python-docx>=1.1"]


def test_frozen_install_deps_noop_when_already_in_runtime(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [python-docx>=1.1]\n")
    installer.install(str(repo))
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: {"python-docx"})
    import runtime.python_install as pi

    monkeypatch.setattr(
        pi,
        "install_requirements_into_managed_runtime",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not install — already satisfied")),
    )
    assert installer.install_deps("demo_ext") == ["python-docx>=1.1"]


def test_frozen_install_deps_refuses_when_runtime_unprovisioned(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [python-docx>=1.1]\n")
    installer.install(str(repo))
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())
    import runtime.python_install as pi

    def _refuse(reqs, **k):
        raise pi.PythonInstallError("the managed Python runtime isn't provisioned — install it first")

    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", _refuse)
    with pytest.raises(installer.InstallError, match="isn't provisioned"):
        installer.install_deps("demo_ext")


def test_frozen_install_deps_optional_only_failure_keeps_satisfied_optionals(env, monkeypatch, caplog):
    """#2162: when no install target is available and only OPTIONAL deps are missing,
    the degrade path must drop ONLY the missing optionals — hard deps AND the
    already-satisfied optionals stay in the return, and the warning still NAMES the
    missing deps (#1953 contract)."""
    import logging as _logging

    repo = _make_plugin_repo(
        env,
        manifest_extra=(
            "requires_pip: [httpx>=0.27, {pkg: 'websockets>=12', optional: true}, "
            "{pkg: 'definitely_not_real_xyz>=1', optional: true}]\n"
        ),
    )
    installer.install(str(repo))
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())
    import runtime.python_install as pi

    def _refuse(reqs, **k):
        raise pi.PythonInstallError("the managed Python runtime isn't provisioned — install it first")

    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", _refuse)
    with caplog.at_level(_logging.WARNING):
        deps = installer.install_deps("demo_ext")  # no raise: only an optional is missing
    assert deps == ["httpx>=0.27", "websockets>=12"]  # satisfied optional kept, missing one dropped
    assert "optional dep(s) definitely_not_real_xyz aren't in the desktop runtime" in caplog.text


# ── frozen install/update gate routes deps into the managed runtime (#2226) ──


def test_frozen_install_pips_missing_deps_into_managed_runtime(env, monkeypatch):
    """#2226: a frozen install/update with a provisioned managed runtime no longer
    refuses on missing hard deps with the pre-ADR-0093 message — it pips them into
    the runtime (the install_deps target) and the install proceeds."""
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [python-docx>=1.1]\n")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FETCH", "git")  # frozen forces archive fetch; keep the local-repo clone
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())  # dep not satisfied anywhere
    import infra.python_runtime as pr
    import runtime.python_install as pi

    monkeypatch.setattr(pr, "managed_python_exe", lambda: Path("/fake/runtime/bin/python3"))
    got = {}
    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", lambda reqs, **k: got.setdefault("reqs", reqs))
    summary = installer.install(str(repo))
    assert got["reqs"] == ["python-docx>=1.1"]  # wheel install attempted, into the managed runtime
    assert summary["id"] == "demo_ext"
    assert (installer.live_plugins_dir() / "demo_ext" / "protoagent.plugin.yaml").exists()


def test_frozen_install_refuses_without_managed_runtime_and_names_the_install_route(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [python-docx>=1.1]\n")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FETCH", "git")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())
    import infra.python_runtime as pr
    import runtime.python_install as pi

    monkeypatch.setattr(pr, "managed_python_exe", lambda: None)  # runtime absent
    monkeypatch.setattr(
        pi,
        "install_requirements_into_managed_runtime",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not attempt an install without a runtime")),
    )
    with pytest.raises(installer.InstallError, match="POST /api/runtime/python/install"):
        installer.install(str(repo))
    assert not (installer.live_plugins_dir() / "demo_ext").exists()  # refused before landing code


def test_frozen_install_surfaces_the_real_error_when_runtime_install_fails(env, monkeypatch):
    repo = _make_plugin_repo(env, manifest_extra="requires_pip: [python-docx>=1.1]\n")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FETCH", "git")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())
    import infra.python_runtime as pr
    import runtime.python_install as pi

    monkeypatch.setattr(pr, "managed_python_exe", lambda: Path("/fake/runtime/bin/python3"))

    def _boom(reqs, **k):
        raise pi.PythonInstallError(
            "plugin dependency install failed: No matching distribution found for python-docx>=1.1"
        )

    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", _boom)
    with pytest.raises(installer.InstallError, match="No matching distribution found"):
        installer.install(str(repo))
    assert not (installer.live_plugins_dir() / "demo_ext").exists()


def test_managed_runtime_dists_read_failure_degrades_to_empty(monkeypatch, caplog):
    """A broken managed-runtime read must never break dep resolution — the fallback
    is 'nothing installed there' (empty set), with the swallow left visible in the log."""
    import logging as _logging

    import infra.python_runtime as pr

    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(
        pr,
        "managed_runtime_distributions",
        lambda: (_ for _ in ()).throw(OSError("unreadable site-packages")),
    )
    with caplog.at_level(_logging.DEBUG, logger="graph.plugins.installer"):
        assert installer._managed_runtime_dists() == set()
    assert "managed runtime read failed" in caplog.text


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
    lines += [
        "enabled: [delegates, demo_a, demo_b]",
        "config:",
        "  demo_a: { k: v }",
        "mcp:",
        "  - { template: github, inputs: [ { key: token, label: Token } ] }",
        "secrets:",
        "  - { key: api_key, label: API Key, secret: true, required: true }",
        "config_inputs:",
        "  - { key: demo_a.board_repo, label: Board repo, type: string, required: true }",
        "  - { key: demo_a.workroot, label: Work root, type: path, default: ~/work }",
        "  - { key: demo_a.auto_merge, label: Auto merge, type: boolean, default: false }",
    ]
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
    # display name persisted so the console can label member rows without re-parsing
    # the bundle manifest (older locks lack it — consumers fall back to the id)
    assert bundles[0]["name"] == "Demo Stack"
    assert set(bundles[0]["plugins"]) == {"demo_a", "demo_b"}
    # the curated turn-on list is persisted in the lock (#1346) so a lock-only consumer
    # (the fleet new-agent path) can auto-enable exactly what the author intended.
    assert bundles[0]["enabled"] == ["delegates", "demo_a", "demo_b"]
    # ...and the recommended config defaults too (#1350), for the same consumer.
    assert bundles[0]["config"] == {"demo_a": {"k": "v"}}
    # MCP servers + declared secrets are cached in the lock (#2041) so the lock-only
    # create path can seed / prompt for these inputs without re-parsing the bundle.
    assert bundles[0]["mcp"] == [{"template": "github", "inputs": [{"key": "token", "label": "Token"}]}]
    assert bundles[0]["secrets"] == [{"key": "api_key", "label": "API Key", "secret": True, "required": True}]
    # config_inputs (#2934): normalized + cached in the lock AND surfaced in the summary,
    # so both the live install path and the lock-only create path can prompt/write them.
    expected_config_inputs = [
        {"key": "demo_a.board_repo", "label": "Board repo", "type": "string", "required": True},
        {"key": "demo_a.workroot", "label": "Work root", "type": "path", "required": False, "default": "~/work"},
        {"key": "demo_a.auto_merge", "label": "Auto merge", "type": "boolean", "required": False, "default": False},
    ]
    assert summary["config_inputs"] == expected_config_inputs
    assert bundles[0]["config_inputs"] == expected_config_inputs


# ── Bundle lifecycle past install (ADR 0049 D4, #2718) ─────────────────────────


def test_check_bundle_updates_reports_behind_when_bundle_repo_moves(env):
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))

    rows = installer.check_bundle_updates()
    assert len(rows) == 1 and rows[0]["id"] == "demo_stack" and rows[0]["behind"] is False

    # a new commit on the bundle repo = the manifest moved → behind (fresh TTL key not
    # needed: the cache stores the FIRST answer, so bust it by expiring the entry)
    (bundle / "README.md").write_text("bump\n")
    _git(bundle, "add", "-A")
    _git(bundle, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "bump")
    installer._lsremote_cache.clear()
    rows = installer.check_bundle_updates()
    assert rows[0]["behind"] is True and rows[0]["latest_sha"]


def test_update_via_force_reinstall_repins_moved_member(env):
    """The bundle-update primitive: a force re-install of the bundle re-resolves each
    member at its ref — a member repo that moved gets a NEW pinned SHA in the lock."""
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))
    old_sha = next(e for e in installer._read_lock()["plugins"] if e["id"] == "demo_a")["resolved_sha"]

    (a / "__init__.py").write_text("def register(registry):\n    pass  # v2\n")
    _git(a, "add", "-A")
    _git(a, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v2")

    installer.install(str(bundle), force=True, by="cli-update-bundle:demo_stack")
    new_sha = next(e for e in installer._read_lock()["plugins"] if e["id"] == "demo_a")["resolved_sha"]
    assert new_sha != old_sha


def test_orphaned_bundle_members_after_manifest_drop(env):
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))
    before = list(installer.bundle_entry("demo_stack")["plugins"])
    assert set(before) == {"demo_a", "demo_b"}

    # the new manifest revision drops demo_b
    manifest = (bundle / "protoagent.bundle.yaml").read_text()
    (bundle / "protoagent.bundle.yaml").write_text(
        "\n".join(ln for ln in manifest.splitlines() if "src-demo_b" not in ln) + "\n"
    )
    _git(bundle, "add", "-A")
    _git(bundle, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "drop b")
    installer.install(str(bundle), force=True)

    assert installer.orphaned_bundle_members("demo_stack", before) == ["demo_b"]
    # …and demo_a, still in the manifest, is not orphaned
    assert "demo_a" in installer.bundle_entry("demo_stack")["plugins"]


def test_uninstall_bundle_removes_exclusive_keeps_shared(env):
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))

    # simulate a SECOND bundle that also lists demo_b → demo_b is shared, kept
    lock = installer._read_lock()
    lock["bundles"].append({"id": "other_stack", "source_url": "https://x/other", "plugins": ["demo_b"]})
    installer._write_lock(lock)

    rep = installer.uninstall_bundle("demo_stack")
    assert rep["removed_members"] == ["demo_a"] and rep["kept"] == ["demo_b"]
    assert not (installer.live_plugins_dir() / "demo_a").exists()
    assert (installer.live_plugins_dir() / "demo_b").exists()
    lock = installer._read_lock()
    assert [b_["id"] for b_ in lock["bundles"]] == ["other_stack"]  # row gone, other intact
    assert {e["id"] for e in lock["plugins"]} == {"demo_b"}


def test_uninstall_bundle_tolerates_individually_removed_member(env):
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))
    installer.uninstall("demo_a")  # provenance row still lists demo_a (#2718's stale-row gap)

    rep = installer.uninstall_bundle("demo_stack")
    assert rep["removed_members"] == ["demo_b"]
    # The 2732 review's bucketing finding: the already-gone member must land in
    # skipped_missing — reporting it as "kept" claimed a plugin that isn't installed.
    assert rep["skipped_missing"] == ["demo_a"]
    assert rep["kept"] == []
    assert installer.bundle_entry("demo_stack") is None


def test_uninstall_bundle_unknown_id_raises(env):
    # The TYPED subclass — HTTP adapters map it to 404 without string-matching,
    # closing the pre-check TOCTOU the 2740 review flagged.
    with pytest.raises(installer.BundleNotInstalledError):
        installer.uninstall_bundle("nope")


def test_uninstall_bundle_reports_raised_failures_separately(env, monkeypatch):
    """An uninstall that RAISED is not "already gone" — it lands in `failed`, never
    skipped_missing (2740 review nit)."""
    a = _make_plugin_repo(env, pid="demo_a")
    b = _make_plugin_repo(env, pid="demo_b")
    bundle = _make_bundle_repo(env, [a, b])
    installer.install(str(bundle))

    real_uninstall = installer.uninstall

    def flaky(pid, **k):
        if pid == "demo_b":
            raise installer.InstallError("demo_b is wedged")
        return real_uninstall(pid, **k)

    monkeypatch.setattr(installer, "uninstall", flaky)
    rep = installer.uninstall_bundle("demo_stack")
    assert rep["removed_members"] == ["demo_a"]
    assert rep["failed"] == {"demo_b": "demo_b is wedged"}
    assert rep["skipped_missing"] == []


def test_archetype_block_warns_on_unknown_keys(caplog):
    """The archetype: block is cached verbatim into plugins.lock and consumed field-by-
    field — a typo'd key used to vanish with no signal anywhere (#2715). Now it warns
    at install (never fails: existing bundles must keep installing)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="graph.plugins.installer"):
        arch = installer._checked_archetype_block(
            "demo_stack", {"label": "Demo", "souls": "typo", "require_tools": ["x"]}
        )
    assert arch == {"label": "Demo", "souls": "typo", "require_tools": ["x"]}  # cached as-is
    warning = "\n".join(r.message for r in caplog.records)
    assert "demo_stack" in warning and "require_tools" in warning and "souls" in warning


def test_archetype_block_known_keys_stay_silent(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="graph.plugins.installer"):
        installer._checked_archetype_block(
            "demo_stack",
            {
                "label": "Demo",
                "icon": "Boxes",
                "blurb": "x",
                "soul": "p",
                "soul_preset": "base",
                "tier": "advanced",
                "requires": ["python_runtime"],
                "requires_tools": ["github_create_issue"],
            },
        )
    assert not caplog.records


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


# ── config_inputs — declared operator prompts in the bundle manifest (#2934) ──
def test_normalize_config_inputs_normalizes_and_coerces():
    """Valid entries normalize to {key, label, type, required, default?}: type defaults
    to string, required coerces to bool, and a boolean default parses words — a quoted
    "false" must come out False, never truthy."""
    entries = installer.normalize_config_inputs(
        "stack",
        [
            {"key": "board.repo", "label": "Repo"},
            {"key": "board.auto", "label": "Auto", "type": "boolean", "required": 1, "default": "false"},
            {"key": "acp.default_delegate", "label": "Delegate", "type": "delegate"},
        ],
    )
    assert entries == [
        {"key": "board.repo", "label": "Repo", "type": "string", "required": False},
        {"key": "board.auto", "label": "Auto", "type": "boolean", "required": True, "default": False},
        {"key": "acp.default_delegate", "label": "Delegate", "type": "delegate", "required": False},
    ]
    assert installer.normalize_config_inputs("stack", None) == []


@pytest.mark.parametrize(
    "entry",
    [
        {"label": "no key"},
        {"key": "toplevel", "label": "not a dotted path"},  # a bare key would replace a whole section
        {"key": "a..b", "label": "empty segment"},
        {"key": "board.repo"},  # no label — nothing to prompt with
        {"key": "board.repo", "label": "Repo", "type": "dropdown"},  # unknown type
        "not-a-mapping",
    ],
)
def test_normalize_config_inputs_strict_rejects_malformed(entry):
    """The install path is strict: a typo'd manifest fails the install with a reason
    instead of silently dropping the operator prompt."""
    with pytest.raises(installer.InstallError):
        installer.normalize_config_inputs("stack", [entry])


def test_normalize_config_inputs_lenient_drops_malformed():
    """The read-only peek is lenient: one bad entry drops, the rest still preview."""
    entries = installer.normalize_config_inputs(
        "stack", [{"key": "ok.key", "label": "OK"}, {"key": "bad", "label": "Bad"}], strict=False
    )
    assert [e["key"] for e in entries] == ["ok.key"]
    assert installer.normalize_config_inputs("stack", "nonsense", strict=False) == []


def test_install_bundle_rejects_malformed_config_inputs(env):
    """A bundle whose config_inputs block is malformed fails BEFORE any member fetch."""
    repo = env / "src-badinputs"
    repo.mkdir(parents=True)
    (repo / "protoagent.bundle.yaml").write_text(
        "id: bad_inputs\nplugins:\n  - { id: delegates, builtin: true }\n"
        "config_inputs:\n  - { key: notdotted, label: X }\n"
    )
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    with pytest.raises(installer.InstallError, match="config_inputs"):
        installer.install(str(repo))


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
    assert installer._git_auth_env("git@github.com:o/r.git") == {}  # ssh → git's own auth
    assert installer._git_auth_env("https://gitlab.com/o/r") == {}  # non-github
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert installer._git_auth_env("https://github.com/o/r") == {}  # no token


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
    monkeypatch.setattr(
        installer,
        "_git",
        lambda *a, cwd=None, timeout=None, env=None: calls.append({"args": a, "env": env}) or "b" * 40,
    )
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
    monkeypatch.setattr(
        installer,
        "_git",
        lambda *a, cwd=None, timeout=None, env=None: cap.update(args=a, env=env) or "sha\trefs/tags/v0.1.0",
    )
    installer._lstags_cache.clear()
    installer._ls_remote_tags("https://github.com/protoLabsAI/private-plugin")
    assert cap["env"] and cap["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


def test_ls_remote_no_auth_env_without_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    cap = {}
    monkeypatch.setattr(
        installer, "_git", lambda *a, cwd=None, timeout=None, env=None: cap.update(env=env) or "sha\tHEAD"
    )
    installer._lsremote_cache.clear()
    installer._ls_remote_sha("https://github.com/protoLabsAI/public-plugin", "")
    assert cap["env"] is None  # no token → plain env, unchanged behavior


# ── absent-open vs explicit-empty-deny (#2743 item 1) ─────────────────────────


def test_source_allowed_absent_is_open_explicit_empty_denies():
    from graph.plugins.installer import _source_allowed

    url = "https://github.com/someone/some-plugin"
    assert _source_allowed(url, None) is True  # key absent — the documented open default
    assert _source_allowed(url, []) is False  # explicit [] — deny-all, the hardening stance
    assert _source_allowed(url, ["github.com/protoLabsAI/*"]) is False
    assert _source_allowed("https://github.com/protoLabsAI/x", ["github.com/protoLabsAI/*"]) is True


def test_install_names_deny_all_distinctly(tmp_path, monkeypatch):
    from graph.plugins.installer import InstallError, install

    try:
        install("https://github.com/someone/some-plugin", allow=[])
        raise AssertionError("expected InstallError")
    except InstallError as e:
        assert "deny-all" in str(e)


def test_configured_allowlist_preserves_explicit_empty(tmp_path, monkeypatch):
    """The CLI path collapsed explicit [] back to None (open) via `or None` —
    exactly the ambiguity #2743 removes."""
    import graph.plugins.installer as inst

    cfg = tmp_path / "langgraph-config.yaml"
    cfg.write_text("plugins:\n  sources:\n    allow: []\n")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: cfg)
    assert inst.configured_allowlist() == []

    cfg.write_text("plugins:\n  sources: {}\n")
    assert inst.configured_allowlist() is None

    cfg.write_text("plugins:\n  sources:\n    allow: [github.com/protoLabsAI/*]\n")
    assert inst.configured_allowlist() == ["github.com/protoLabsAI/*"]


def test_from_yaml_distinguishes_and_warns_on_explicit_empty(tmp_path, caplog):
    from graph.config import LangGraphConfig

    cfg = tmp_path / "c.yaml"
    cfg.write_text("plugins:\n  sources:\n    allow: []\n")
    with caplog.at_level("WARNING"):
        parsed = LangGraphConfig.from_yaml(str(cfg))
    assert parsed.plugins_sources_allow == []
    assert "DENY-ALL" in caplog.text  # the migration flip is loud, never silent

    cfg.write_text("plugins: {}\n")
    assert LangGraphConfig.from_yaml(str(cfg)).plugins_sources_allow is None
