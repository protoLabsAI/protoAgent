"""``protoagent cli link`` puts the desktop build's own CLI on PATH (v0.231.0 stress test:
``protoagent`` on PATH was a stale v0.207.0 ``uv tool`` install and the app offered no way
to link its CLI). A link is created, refreshed when an earlier build made it, and never
replaces something the operator installed unless ``--force``."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from infra import cli_link
from server.cli import dispatch

# Symlinks in ~/.local/bin are the Unix story; on Windows the verb only says so (last test).
pytestmark = pytest.mark.platform_sensitive
unix_only = pytest.mark.skipif(os.name == "nt", reason="cli link is a Unix symlink; Windows reports unsupported")


@pytest.fixture
def frozen_bin(tmp_path, monkeypatch) -> Path:
    """Pretend to be the frozen desktop binary at a stable app path."""
    app = tmp_path / "protoAgent.app" / "Contents" / "Resources" / "protoagent-server"
    app.mkdir(parents=True)
    exe = app / "protoagent-server"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delenv("APPIMAGE", raising=False)
    return exe.resolve()


def _run(argv: list[str], capsys) -> tuple[int, dict]:
    code = dispatch(["cli", *argv, "--json"])
    return code, json.loads(capsys.readouterr().out)


@unix_only
def test_link_creates_symlink_to_the_running_binary(tmp_path, frozen_bin, capsys, monkeypatch):
    bindir = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(bindir))
    code, res = _run(["link", "--dir", str(bindir)], capsys)
    link = bindir / "protoagent"
    assert code == 0 and res["action"] == "created"
    assert link.is_symlink() and link.resolve() == frozen_bin
    assert res["path"]["dir_on_path"] is True
    assert res["path"]["is_this_build"] is True

    # Second run is a no-op.
    code, res = _run(["link", "--dir", str(bindir)], capsys)
    assert code == 0 and res["action"] == "none" and res["state"] == "linked"


@unix_only
def test_link_replaces_an_earlier_builds_dangling_link(tmp_path, frozen_bin, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    old = tmp_path / "old.app" / "Contents" / "MacOS" / "protoagent-server"  # gone now
    (bindir / "protoagent").symlink_to(old)
    code, res = _run(["link", "--dir", str(bindir)], capsys)
    assert code == 0 and res["action"] == "replaced"
    assert (bindir / "protoagent").resolve() == frozen_bin


@unix_only
def test_link_refuses_a_uv_tool_install_without_force(tmp_path, frozen_bin, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = tmp_path / ".local" / "share" / "uv" / "tools" / "protolabs-agent" / "bin" / "protoagent"
    uv.parent.mkdir(parents=True)
    uv.write_text("#!/bin/sh\n")
    (bindir / "protoagent").symlink_to(uv)

    code, res = _run(["link", "--dir", str(bindir)], capsys)
    assert code == 1 and res["action"] == "refused"
    assert "uv tool uninstall protolabs-agent" in res["reason"]
    assert (bindir / "protoagent").resolve() == uv.resolve()  # untouched

    code, res = _run(["link", "--dir", str(bindir), "--force"], capsys)
    assert code == 0 and res["action"] == "replaced"
    assert (bindir / "protoagent").resolve() == frozen_bin
    assert uv.exists()  # the uv tool's own files are left alone


@unix_only
def test_link_refuses_a_plain_file(tmp_path, frozen_bin, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "protoagent").write_text("#!/bin/sh\n")
    code, res = _run(["link", "--dir", str(bindir)], capsys)
    assert code == 1 and res["action"] == "refused"
    assert not (bindir / "protoagent").is_symlink()


@unix_only
def test_unlink_removes_only_our_link(tmp_path, frozen_bin, capsys):
    bindir = tmp_path / "bin"
    _run(["link", "--dir", str(bindir)], capsys)
    code, res = _run(["unlink", "--dir", str(bindir)], capsys)
    assert code == 0 and res["action"] == "removed"
    assert not os.path.lexists(bindir / "protoagent")

    (bindir / "protoagent").write_text("mine")
    code, res = _run(["unlink", "--dir", str(bindir)], capsys)
    assert code == 1 and res["action"] == "refused"
    assert (bindir / "protoagent").read_text() == "mine"


@unix_only
def test_status_reports_a_shadowing_protoagent_earlier_on_path(tmp_path, frozen_bin, capsys, monkeypatch):
    bindir = tmp_path / "bin"
    earlier = tmp_path / "earlier"
    earlier.mkdir()
    stale = earlier / "protoagent"
    stale.write_text("#!/bin/sh\n")
    stale.chmod(0o755)
    _run(["link", "--dir", str(bindir)], capsys)
    monkeypatch.setenv("PATH", os.pathsep.join([str(earlier), str(bindir)]))
    code, res = _run(["status", "--dir", str(bindir)], capsys)
    assert code == 0 and res["state"] == "linked"
    assert res["path"]["is_this_build"] is False
    assert res["path"]["which_resolves_to"] == str(stale.resolve())


@unix_only
def test_a_source_install_is_not_linked(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    code, res = _run(["link", "--dir", str(tmp_path / "bin")], capsys)
    assert code == 2 and res["action"] == "unsupported"
    assert not (tmp_path / "bin").exists()


@unix_only
def test_an_appimage_is_not_linked(tmp_path, frozen_bin, monkeypatch, capsys):
    monkeypatch.setenv("APPIMAGE", "/home/me/protoAgent.AppImage")
    code, res = _run(["link", "--dir", str(tmp_path / "bin")], capsys)
    assert code == 2 and "AppImage" in res["reason"]


def test_cli_is_a_forwarded_verb_in_the_help_tree(capsys):
    from server import cli

    assert cli._FORWARD["cli"] == ("infra.cli_link", "run_cli_link_cli")
    assert "cli" in cli._FORWARD_HELP
    assert cli_link.default_link_dir() == Path.home() / ".local" / "bin"


@pytest.mark.skipif(os.name != "nt", reason="the Windows branch")
def test_link_is_unsupported_on_windows(tmp_path, frozen_bin, capsys):
    code, res = _run(["link", "--dir", str(tmp_path / "bin")], capsys)
    assert code == 2 and res["action"] == "unsupported"


# The tray's "Install Command-Line Tool…" dialog shows the human output verbatim, so it has
# to read as plain copy — not "Linked <link> -> <app bundle internals>".


@unix_only
def test_link_human_output_is_plain_copy(tmp_path, frozen_bin, capsys, monkeypatch):
    bindir = tmp_path / "bin"
    monkeypatch.setenv("PATH", str(bindir))
    assert dispatch(["cli", "link", "--dir", str(bindir)]) == 0
    out = capsys.readouterr().out
    assert out.startswith("The protoagent command is installed.")
    assert "protoagent --help" in out
    assert "->" not in out and "Contents/Resources" not in out
    assert dispatch(["cli", "link", "--dir", str(bindir)]) == 0
    assert capsys.readouterr().out.startswith("The protoagent command is already installed.")


@unix_only
def test_link_human_output_says_how_to_reach_an_off_path_dir(tmp_path, frozen_bin, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    assert dispatch(["cli", "link", "--dir", str(tmp_path / "bin")]) == 0
    out = capsys.readouterr().out
    assert "can't find it yet" in out and "export PATH=" in out
    assert "protoagent --help" not in out


@unix_only
def test_link_human_output_names_a_shadowing_install(tmp_path, frozen_bin, capsys, monkeypatch):
    bindir, earlier = tmp_path / "bin", tmp_path / "earlier"
    earlier.mkdir()
    (earlier / "protoagent").write_text("#!/bin/sh\n")
    (earlier / "protoagent").chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([str(earlier), str(bindir)]))
    assert dispatch(["cli", "link", "--dir", str(bindir)]) == 0
    out = capsys.readouterr().out
    assert "still runs another protoagent first" in out


@unix_only
def test_refused_link_human_output_says_what_to_do(tmp_path, frozen_bin, capsys):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "protoagent").write_text("#!/bin/sh\n")
    assert dispatch(["cli", "link", "--dir", str(bindir)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("There's already a protoagent command at")
    assert "--force" in err
