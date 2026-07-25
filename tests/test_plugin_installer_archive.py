"""Git-less plugin install for the frozen desktop app (ADR 0058) — HTTPS archive
fetch (D1) + the bundled-dep gate (D2). No git, no pip, no network: the fetch
seams (`_resolve_sha_github` / `_http_get`) are monkeypatched."""

from __future__ import annotations

import io
import tarfile

import pytest

from graph.plugins import installer

_SHA = "a" * 40


def _tarball(pid: str = "demo_ext", *, requires_pip: str = "", sha: str = _SHA) -> bytes:
    """A GitHub-style archive: a single ``<repo>-<sha>/`` top dir holding a plugin."""
    manifest = f"id: {pid}\nname: Demo Ext\nversion: 0.1.0\ndescription: a test plugin\n"
    if requires_pip:
        manifest += f"requires_pip: [{requires_pip}]\n"
    files = {"protoagent.plugin.yaml": manifest, "__init__.py": "def register(registry):\n    pass\n"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, body in files.items():
            data = body.encode()
            info = tarfile.TarInfo(name=f"{pid}-{sha}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class _Resp:
    def __init__(self, content: bytes = b"", text: str = ""):
        self.content = content
        self.text = text


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Installer lock + install dir + config/secrets in a temp area; archive fetch
    forced so the test never shells out to git."""
    import graph.config_io as cio

    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    monkeypatch.setenv("PROTOAGENT_PLUGINS_DIR", str(tmp_path / "installed"))
    (tmp_path / "cfg").mkdir()
    monkeypatch.setattr(cio, "config_yaml_path", lambda: tmp_path / "cfg" / "langgraph-config.yaml")
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: tmp_path / "cfg" / "secrets.yaml")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FETCH", "archive")
    return tmp_path


# --- pure helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    "url,owner,repo",
    [
        ("https://github.com/protoLabsAI/discord-plugin", "protoLabsAI", "discord-plugin"),
        ("https://github.com/protoLabsAI/discord-plugin.git", "protoLabsAI", "discord-plugin"),
        ("https://github.com/protoLabsAI/discord-plugin/", "protoLabsAI", "discord-plugin"),
        ("git@github.com:protoLabsAI/discord-plugin.git", "protoLabsAI", "discord-plugin"),
    ],
)
def test_github_owner_repo_parsing(url, owner, repo):
    assert installer._github_owner_repo(url) == (owner, repo)


def test_github_owner_repo_rejects_non_github():
    with pytest.raises(installer.InstallError, match="github.com URL"):
        installer._github_owner_repo("https://gitlab.com/x/y")


@pytest.mark.parametrize("ref", ["main", "release/1.2", "v1.0.0", "a" * 40, "feature_x"])
def test_validate_ref_accepts_real_refs(ref):
    installer._validate_ref(ref)  # no raise


@pytest.mark.parametrize("ref", ["../../etc/passwd", "main..evil", "-upload-pack=x", "a b", "x?y=z", "/abs", "a#frag"])
def test_validate_ref_rejects_unsafe(ref):
    with pytest.raises(installer.InstallError, match="invalid ref"):
        installer._validate_ref(ref)


def test_install_rejects_unsafe_ref_before_fetch(env, monkeypatch):
    # Refused at validation — never reaches the GitHub API URL or git.
    called = {"fetch": False}
    monkeypatch.setattr(installer, "_fetch", lambda *a, **k: called.__setitem__("fetch", True))
    with pytest.raises(installer.InstallError, match="invalid ref"):
        installer.install("https://github.com/acme/demo_ext", "../../evil")
    assert called["fetch"] is False


@pytest.mark.parametrize("spec,name", [("websockets>=12", "websockets"), ("httpx", "httpx"), ("pkg[extra]>=1", "pkg")])
def test_dep_pkg_name(spec, name):
    assert installer._dep_pkg_name(spec) == name


def test_normalize_dist_is_public_and_shared():
    # The installer's dep resolution normalizes through infra's PUBLIC helper —
    # one PEP 503 notion of a dist name, so the two can never disagree.
    from infra.python_runtime import normalize_dist

    assert normalize_dist("Python_DocX") == "python-docx"
    assert installer._normalize_dist("Python_DocX") == normalize_dist("Python_DocX")


def test_deps_satisfied_against_runtime():
    # httpx + websockets are core deps (always importable in this test runtime).
    assert installer._deps_satisfied(["httpx>=0.27", "websockets>=12"]) == (True, [])
    ok, missing = installer._deps_satisfied(["definitely_not_a_real_pkg_xyz>=1"])
    assert not ok and missing == ["definitely_not_a_real_pkg_xyz"]


def test_safe_extract_strips_top_dir(tmp_path):
    dest = tmp_path / "out"
    installer._safe_extract_tar(_tarball(), dest)
    assert (dest / "protoagent.plugin.yaml").exists()
    assert (dest / "__init__.py").exists()
    assert not (dest / f"demo_ext-{_SHA}").exists()  # top component stripped


def test_safe_extract_blocks_traversal(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="repo-sha/../../escape.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(installer.InstallError, match="unsafe path"):
        installer._safe_extract_tar(buf.getvalue(), tmp_path / "out")


# --- install() through the archive path -----------------------------------


def test_install_via_archive_pins_sha_and_writes_lock(env, monkeypatch):
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(installer, "_http_get", lambda url, **kw: _Resp(content=_tarball()))

    summary = installer.install("https://github.com/acme/demo_ext")

    assert summary["id"] == "demo_ext"
    assert summary["resolved_sha"] == _SHA
    target = installer.live_plugins_dir() / "demo_ext"
    assert (target / "protoagent.plugin.yaml").exists()
    locked = installer.list_installed()
    assert locked[0]["id"] == "demo_ext" and locked[0]["resolved_sha"] == _SHA


def test_frozen_dep_gate_refuses_unbundled_dep(env, monkeypatch):
    """#2226: the gate refuses only when NO managed runtime is provisioned — and the
    message points at the runtime install instead of 'use a server build'."""
    import infra.python_runtime as pr

    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(pr, "managed_python_exe", lambda: None)  # runtime absent
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(
        installer, "_http_get", lambda url, **kw: _Resp(content=_tarball(requires_pip="definitely_not_real_xyz>=1"))
    )
    with pytest.raises(installer.InstallError, match="POST /api/runtime/python/install"):
        installer.install("https://github.com/acme/demo_ext")
    assert not (installer.live_plugins_dir() / "demo_ext").exists()  # refused before landing


def test_frozen_install_ok_when_deps_bundled(env, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(installer, "_http_get", lambda url, **kw: _Resp(content=_tarball(requires_pip="httpx>=0.27")))
    summary = installer.install("https://github.com/acme/demo_ext")
    assert summary["id"] == "demo_ext"
    assert (installer.live_plugins_dir() / "demo_ext").exists()


def test_install_deps_frozen_skips_pip_when_bundled(env, monkeypatch):
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(installer, "_http_get", lambda url, **kw: _Resp(content=_tarball(requires_pip="httpx>=0.27")))
    installer.install("https://github.com/acme/demo_ext")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    # Would raise if it shelled out to pip; instead the gate sees httpx is bundled.
    assert installer.install_deps("demo_ext") == ["httpx>=0.27"]


# --- optional dep tier (#1953): the D2 gate warns instead of refusing ---------

_SOFT_MISSING = "{pkg: definitely_not_real_xyz>=1, optional: true}"


def test_frozen_missing_optional_dep_warns_and_installs(env, monkeypatch, caplog):
    """A missing OPTIONAL dep must not gate the frozen install — it lands with a
    visible warning naming the dep (the protobanana/pillow case)."""
    import logging as _logging

    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(
        installer, "_http_get", lambda url, **kw: _Resp(content=_tarball(requires_pip=f"httpx>=0.27, {_SOFT_MISSING}"))
    )
    with caplog.at_level(_logging.WARNING):
        summary = installer.install("https://github.com/acme/demo_ext")
    assert summary["id"] == "demo_ext"
    assert (installer.live_plugins_dir() / "demo_ext").exists()  # installed, not refused
    assert summary["optional_pip"] == ["definitely_not_real_xyz>=1"]
    assert any("definitely_not_real_xyz" in w for w in summary["warnings"])  # warning in the result
    assert "definitely_not_real_xyz" in caplog.text  # ...and in the log


def test_frozen_missing_hard_dep_still_refuses_with_optional_present(env, monkeypatch):
    """Hard wins: a mixed manifest with a missing hard dep still refuses when no
    managed runtime is provisioned — a missing optional alone wouldn't (#2226)."""
    import infra.python_runtime as pr

    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(pr, "managed_python_exe", lambda: None)  # runtime absent
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(
        installer,
        "_http_get",
        lambda url, **kw: _Resp(content=_tarball(requires_pip=f"also_not_real_abc>=1, {_SOFT_MISSING}")),
    )
    with pytest.raises(installer.InstallError, match="isn't in the desktop runtime"):
        installer.install("https://github.com/acme/demo_ext")
    assert not (installer.live_plugins_dir() / "demo_ext").exists()


def test_frozen_satisfied_optional_dep_no_warning(env, monkeypatch):
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(
        installer,
        "_http_get",
        lambda url, **kw: _Resp(content=_tarball(requires_pip="{pkg: websockets>=12, optional: true}")),
    )
    summary = installer.install("https://github.com/acme/demo_ext")
    assert "warnings" not in summary  # nothing missing → nothing to warn about


def test_install_deps_frozen_missing_optional_warns_not_refuses(env, monkeypatch, caplog):
    import logging as _logging

    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(
        installer, "_http_get", lambda url, **kw: _Resp(content=_tarball(requires_pip=f"httpx>=0.27, {_SOFT_MISSING}"))
    )
    installer.install("https://github.com/acme/demo_ext")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    with caplog.at_level(_logging.WARNING):
        deps = installer.install_deps("demo_ext")  # no raise
    assert deps == ["httpx>=0.27"]  # only the satisfied deps
    assert "definitely_not_real_xyz" in caplog.text


def test_install_deps_frozen_optional_only_manifest_keeps_satisfied_optionals(env, monkeypatch, caplog):
    """#2162: an optional-ONLY manifest (no hard deps) hitting the no-target degrade
    path must return the satisfied optionals, dropping just the missing ones."""
    import logging as _logging

    monkeypatch.setattr(installer, "_resolve_sha_github", lambda o, r, ref: _SHA)
    monkeypatch.setattr(
        installer,
        "_http_get",
        lambda url, **kw: _Resp(
            content=_tarball(requires_pip=f"{{pkg: websockets>=12, optional: true}}, {_SOFT_MISSING}")
        ),
    )
    installer.install("https://github.com/acme/demo_ext")
    monkeypatch.setenv("PROTOAGENT_PLUGIN_FROZEN", "1")
    monkeypatch.setattr(installer, "_managed_runtime_dists", lambda: set())
    import runtime.python_install as pi

    def _refuse(reqs, **k):
        raise pi.PythonInstallError("the managed Python runtime isn't provisioned — install it first")

    monkeypatch.setattr(pi, "install_requirements_into_managed_runtime", _refuse)
    with caplog.at_level(_logging.WARNING):
        deps = installer.install_deps("demo_ext")  # no raise: nothing hard is missing
    assert deps == ["websockets>=12"]  # satisfied optional kept; only the missing one dropped
    assert "optional dep(s) definitely_not_real_xyz aren't in the desktop runtime" in caplog.text
