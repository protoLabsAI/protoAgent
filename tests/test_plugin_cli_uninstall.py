"""`plugin uninstall` liveness warning (#2717) — the CLI runs OUT-OF-PROCESS, so a
running server keeps an uninstalled plugin's tools/routers live until restart/reload.
The CLI can't do the live teardown the console route does; it must at least say so."""

from __future__ import annotations

from graph.plugins import cli, installer


def _fake_uninstall(monkeypatch):
    monkeypatch.setattr(
        installer,
        "uninstall",
        lambda pid, purge=False: {
            "id": pid,
            "removed": ["code", "lock", "enabled-ref"],
            "deps_left": [],
            "purged": purge,
            "jobs_cancelled": 0,
        },
    )


def test_uninstall_of_enabled_plugin_warns_about_live_server(monkeypatch, capsys):
    _fake_uninstall(monkeypatch)
    monkeypatch.setattr(cli, "_enabled_in_live_config", lambda pid: True)
    monkeypatch.setattr(cli, "_live_servers", lambda: [{"pid": 4242, "port": 7870, "identity": "protoagent"}])

    assert cli.run_plugin_cli(["uninstall", "demo"]) == 0
    out = capsys.readouterr().out
    assert "✓ uninstalled demo" in out
    assert "RUNNING" in out and "pid 4242" in out and "port 7870" in out
    assert "stays loaded" in out and "Settings ▸ Plugins" in out


def test_uninstall_with_no_live_server_stays_quiet(monkeypatch, capsys):
    _fake_uninstall(monkeypatch)
    monkeypatch.setattr(cli, "_enabled_in_live_config", lambda pid: True)
    monkeypatch.setattr(cli, "_live_servers", lambda: [])

    assert cli.run_plugin_cli(["uninstall", "demo"]) == 0
    assert "RUNNING" not in capsys.readouterr().out


def test_uninstall_of_disabled_plugin_stays_quiet(monkeypatch, capsys):
    """A plugin that wasn't in plugins.enabled isn't live anywhere — warning would be noise."""
    _fake_uninstall(monkeypatch)
    monkeypatch.setattr(cli, "_enabled_in_live_config", lambda pid: False)
    monkeypatch.setattr(cli, "_live_servers", lambda: [{"pid": 4242, "port": 7870, "identity": "protoagent"}])

    assert cli.run_plugin_cli(["uninstall", "demo"]) == 0
    assert "RUNNING" not in capsys.readouterr().out


def test_enabled_in_live_config_reads_yaml(monkeypatch, tmp_path):
    cfg = tmp_path / "langgraph-config.yaml"
    cfg.write_text("plugins:\n  enabled: [alpha, beta]\n")
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: cfg)
    assert cli._enabled_in_live_config("alpha") is True
    assert cli._enabled_in_live_config("ghost") is False


def test_enabled_in_live_config_missing_file_is_false(monkeypatch, tmp_path):
    monkeypatch.setattr("graph.config_io.config_yaml_path", lambda: tmp_path / "nope.yaml")
    assert cli._enabled_in_live_config("alpha") is False
