"""Tests for the `protoagent` CLI (ADR 0075 D1, `server/cli.py`).

The CLI is a thin, discoverable front door: `dispatch()` routes management
subcommands to the existing `graph/**/cli.py` (forwarded verbatim) + the
lifecycle verbs, and returns None for a bare/serve invocation so the server
boots. These tests pin the routing + the lifecycle logic without booting a server.
"""

from __future__ import annotations

import json
import os
import socket
import types

import pytest

from server import cli


def test_dispatch_forwards_management_subcommand(monkeypatch):
    seen = {}
    fake = types.SimpleNamespace(run_plugin_cli=lambda argv: seen.setdefault("argv", argv) and None or 7)
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: fake)
    rc = cli.dispatch(["plugin", "list", "--json"])
    assert rc == 7  # the forwarded CLI's exit code is returned verbatim
    assert seen["argv"] == ["list", "--json"]  # only the args after the subcommand


def test_dispatch_none_when_not_a_subcommand():
    # A bare invocation, server flags, and `serve`/`setup` are NOT dispatch's job —
    # it returns None so the caller boots the server / main() handles serve|setup.
    assert cli.dispatch([]) is None
    assert cli.dispatch(["--port", "7870"]) is None
    assert cli.dispatch(["serve"]) is None
    assert cli.dispatch(["setup"]) is None


def test_dispatch_forward_none_result_is_zero(monkeypatch):
    fake = types.SimpleNamespace(run_config_cli=lambda argv: None)  # a CLI that returns None
    monkeypatch.setattr(cli.importlib, "import_module", lambda name: fake)
    assert cli.dispatch(["config", "explain"]) == 0  # None → 0


def test_main_bare_prints_help(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert "protoagent" in out and "plugin" in out and "up" in out  # the command tree


def test_main_unknown_command(capsys):
    assert cli.main(["bogus"]) == 2
    assert "unknown command" in capsys.readouterr().err


def test_main_serve_boots_server(monkeypatch):
    booted = {}
    monkeypatch.setattr(cli, "_boot_server", lambda argv: booted.update(argv=argv) or 0)
    assert cli.main(["serve", "--port", "7999"]) == 0
    assert booted["argv"] == ["--port", "7999"]


def test_main_setup_maps_to_setup_flag(monkeypatch):
    booted = {}
    monkeypatch.setattr(cli, "_boot_server", lambda argv: booted.update(argv=argv) or 0)
    assert cli.main(["setup"]) == 0
    assert booted["argv"] == ["--setup"]


def test_status_stopped_returns_3(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "_pid_path", lambda: tmp_path / "none.pid")
    # port 1 is not accepting connections here → "stopped"
    assert cli._cmd_status(["--port", "1"]) == 3


def test_status_running_returns_0(monkeypatch, tmp_path, capsys):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        pidf = tmp_path / "server.pid"
        pidf.write_text(json.dumps({"pid": os.getpid(), "port": port, "version": "9.9.9"}), encoding="utf-8")
        monkeypatch.setattr(cli, "_pid_path", lambda: pidf)
        assert cli._cmd_status([]) == 0
        assert f":{port}" in capsys.readouterr().out
    finally:
        srv.close()


def test_up_noop_when_already_running(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_port_open", lambda port, *a, **k: True)  # pretend it's up
    assert cli._cmd_up(["--port", "7870"]) == 0
    assert "already running" in capsys.readouterr().out


def test_down_reports_when_nothing_to_stop(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "_pid_path", lambda: tmp_path / "none.pid")
    monkeypatch.setattr(cli, "_port_open", lambda port, *a, **k: False)
    assert cli._cmd_down([]) == 0
    assert "not running" in capsys.readouterr().out


@pytest.mark.parametrize("frozen,expected_tail", [(False, ["-m", "server"]), (True, [])])
def test_server_base_argv_frozen_awareness(monkeypatch, frozen, expected_tail):
    monkeypatch.setattr(cli.sys, "frozen", frozen, raising=False)
    argv = cli._server_base_argv()
    if frozen:
        assert argv == [cli.sys.executable]
    else:
        assert argv[-2:] == expected_tail


def test_help_lists_every_command(capsys):
    # A regression guard: every management + lifecycle verb must appear in --help,
    # so the CLI never silently gains an undiscoverable command again.
    cli.main(["--help"])
    out = capsys.readouterr().out
    for name in ("serve", "up", "down", "status", "setup", "plugin", "workspace", "skills", "fleet", "config"):
        assert name in out
