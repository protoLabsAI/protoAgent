"""``infra.proc.child_env`` — a PyInstaller-frozen server must not hand its temporary
``_MEIPASS`` paths to external children.

The desktop sidecar exports ``SSL_CERT_FILE`` at its bundled certifi (inside the
``_MEI…`` extraction dir, deleted on exit). An editor launched by ``open_in_editor``
outlived the server, kept the dangling path, and passed it to every process it
started — Zed's ACP agent then died in httpx's ``create_ssl_context`` with
``FileNotFoundError``. These tests pin the scrub at the helper and at each spawn site."""

from __future__ import annotations

import os
import sys

import pytest

from infra.proc import child_env

MEI = os.path.join(os.sep, "var", "folders", "xx", "T", "_MEIabc123")


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", MEI, raising=False)
    return MEI


def _bundle_env() -> dict[str, str]:
    ca = os.path.join(MEI, "certifi", "cacert.pem")
    return {
        "HOME": "/Users/op",
        "PATH": os.pathsep.join(["/usr/bin", os.path.join(MEI, "bin"), "/bin"]),
        "SSL_CERT_FILE": ca,
        "REQUESTS_CA_BUNDLE": ca,
        "CURL_CA_BUNDLE": ca,
        "SSL_CERT_DIR": os.path.join(MEI, "certs"),
        "LD_LIBRARY_PATH": MEI,
        "LD_LIBRARY_PATH_ORIG": "/opt/lib",
        "DYLD_LIBRARY_PATH": MEI,
        "_PYI_ARCHIVE_FILE": "/Applications/protoAgent.app/Contents/MacOS/protoagent-server",
        "_PYI_APPLICATION_HOME_DIR": MEI,
        "_PYI_PARENT_PROCESS_LEVEL": "1",
        "_MEIPASS2": MEI,
        "TCL_LIBRARY": os.path.join(MEI, "_tcl_data"),
        "A2A_AUTH_TOKEN": "keep-me",
        "LOOKALIKE": MEI + "-not-inside/x",
    }


def test_frozen_drops_bundle_paths_keeps_the_rest(frozen):
    env = child_env(_bundle_env())
    for gone in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "SSL_CERT_DIR", "DYLD_LIBRARY_PATH",
                 "_PYI_ARCHIVE_FILE", "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL", "_MEIPASS2",
                 "TCL_LIBRARY", "LD_LIBRARY_PATH_ORIG"):
        assert gone not in env, gone
    # Loader path restored from PyInstaller's *_ORIG save, not just dropped.
    assert env["LD_LIBRARY_PATH"] == "/opt/lib"
    # A pathsep list loses only its bundle entries.
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])
    assert env["HOME"] == "/Users/op" and env["A2A_AUTH_TOKEN"] == "keep-me"
    assert env["LOOKALIKE"] == MEI + "-not-inside/x"  # prefix match is by path component


def test_frozen_keeps_an_operator_ca_bundle_outside_the_bundle(frozen):
    env = child_env({"SSL_CERT_FILE": "/etc/corp/ca.pem", "REQUESTS_CA_BUNDLE": os.path.join(MEI, "c.pem")})
    assert env == {"SSL_CERT_FILE": "/etc/corp/ca.pem"}


def test_frozen_empty_orig_means_unset(frozen):
    env = child_env({"LD_LIBRARY_PATH": MEI, "LD_LIBRARY_PATH_ORIG": ""})
    assert "LD_LIBRARY_PATH" not in env and "LD_LIBRARY_PATH_ORIG" not in env


def test_frozen_matches_resolved_bundle_path(monkeypatch, tmp_path):
    real = tmp_path / "real" / "_MEIxyz"
    real.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(tmp_path / "real")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(link / "_MEIxyz"), raising=False)
    # e.g. macOS /var/folders (what _MEIPASS says) vs /private/var/folders (resolved).
    env = child_env({"SSL_CERT_FILE": str(real / "certifi" / "cacert.pem")})
    assert "SSL_CERT_FILE" not in env


def test_not_frozen_is_a_plain_copy(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    base = _bundle_env()
    env = child_env(base)
    assert env == base and env is not base


def test_defaults_to_os_environ_and_never_mutates_it(frozen, monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", os.path.join(MEI, "certifi", "cacert.pem"))
    monkeypatch.setenv("PROTO_CHILD_ENV_PROBE", "1")
    env = child_env()
    assert "SSL_CERT_FILE" not in env and env["PROTO_CHILD_ENV_PROBE"] == "1"
    assert os.environ["SSL_CERT_FILE"].startswith(MEI)  # the server's own env is untouched


# ── spawn sites ──────────────────────────────────────────────────────────────


def _stale_env(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", os.path.join(MEI, "certifi", "cacert.pem"))
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", MEI)
    monkeypatch.setenv("PROTO_CHILD_ENV_PROBE", "kept")


def _assert_scrubbed(env):
    assert "SSL_CERT_FILE" not in env and "_PYI_APPLICATION_HOME_DIR" not in env
    assert env["PROTO_CHILD_ENV_PROBE"] == "kept"


async def test_shell_run_command_scrubs(frozen, monkeypatch):
    import tools.shell as shell

    seen = {}

    async def _fake_exec(*argv, **kw):
        seen.update(kw)
        raise FileNotFoundError

    _stale_env(monkeypatch)
    monkeypatch.setattr(shell.asyncio, "create_subprocess_exec", _fake_exec)
    await shell.run_command(["true"], env={"EXTRA": "1"})
    _assert_scrubbed(seen["env"])
    assert seen["env"]["EXTRA"] == "1"
    seen.clear()
    await shell.run_command(["true"])  # no caller env → still scrubbed, not inherited raw
    _assert_scrubbed(seen["env"])


async def test_gh_cli_scrubs(frozen, monkeypatch):
    import tools.gh_cli as gh

    seen = {}

    async def _fake_exec(*argv, **kw):
        seen.update(kw)
        raise FileNotFoundError

    _stale_env(monkeypatch)
    monkeypatch.setattr(gh.asyncio, "create_subprocess_exec", _fake_exec)
    await gh.run_gh(["--version"])
    _assert_scrubbed(seen["env"])


def test_mcp_inherited_env_scrubs(frozen, monkeypatch):
    from tools.mcp_tools import _inherited_env

    _stale_env(monkeypatch)
    _assert_scrubbed(_inherited_env({}, inherit=None))
    _assert_scrubbed(_inherited_env({}, inherit=True))


def test_acp_launch_env_scrubs(frozen, monkeypatch):
    from plugins.coding_agent.acp_client import _launch_env

    _stale_env(monkeypatch)
    _assert_scrubbed(_launch_env({"DELEGATE": "1"}))
