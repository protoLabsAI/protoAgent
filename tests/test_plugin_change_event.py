"""ADR 0096 D8 — a successful reload that touched plugin state publishes ``plugin.changed``.

The console's ``PluginChangeWatch`` subscribes to ``plugin.#``. Without the publish, an
agent-initiated enable/reload (the devkit's build loop) or an autoupdate is invisible to
every open console until a manual refresh — the runtime-status poll stops permanently
once the graph is loaded, so no refetch ever comes. Publishing on the reload seam covers
every caller at once (devkit tools, the console toggle for OTHER open tabs, autoupdate).
Gating: a bare call is a pure reload (plugins re-exec) and a ``plugins`` config key is an
enable/disable — any other settings save leaves plugin state alone and must stay quiet.
"""

from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(monkeypatch, tmp_path: Path):
    """Point every config layer at tmp files so a save can't touch the real instance."""
    import graph.config_io as cio

    leaf = tmp_path / "langgraph-config.yaml"
    secrets = tmp_path / "secrets.yaml"
    host = tmp_path / "host-config.yaml"

    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "secrets_yaml_path", lambda: secrets)

    import infra.paths as paths

    monkeypatch.setattr(paths, "host_config_path", lambda: host, raising=False)
    return leaf, secrets, host


@pytest.fixture
def bus_events(monkeypatch):
    import server.agent_init as ai

    events: list[tuple[str, dict]] = []

    class _Bus:
        def publish(self, topic, data=None, **kw):
            events.append((topic, data))

    monkeypatch.setattr(ai, "_event_bus", _Bus())
    monkeypatch.setattr(ai, "_sync_autostart_with_config", lambda *_a, **_k: None)
    return events


def test_pure_reload_publishes_plugin_changed(monkeypatch, isolated_config, bus_events):
    import server.agent_init as ai

    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (True, "reloaded"))
    ok, _ = ai._apply_settings_changes()
    assert ok
    assert bus_events == [("plugin.changed", {"scope": "reload"})]


def test_plugins_save_publishes_plugin_changed(monkeypatch, isolated_config, bus_events):
    import server.agent_init as ai

    leaf, _, _ = isolated_config
    leaf.write_text("model:\n  name: m\n")
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (True, "reloaded"))
    ok, _ = ai._apply_settings_changes(config={"plugins": {"enabled": ["hello"]}})
    assert ok
    assert bus_events == [("plugin.changed", {"scope": "plugins"})]


def test_unrelated_save_stays_quiet(monkeypatch, isolated_config, bus_events):
    import server.agent_init as ai

    leaf, _, _ = isolated_config
    leaf.write_text("model:\n  name: m\n")
    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (True, "reloaded"))
    ok, _ = ai._apply_settings_changes(config={"identity": {"name": "zed"}})
    assert ok
    assert bus_events == []


def test_failed_reload_stays_quiet(monkeypatch, isolated_config, bus_events):
    """A failed reload committed nothing (the rollback contract) — announcing a change
    that didn't happen would make every console refetch identical state."""
    import server.agent_init as ai

    monkeypatch.setattr(ai, "_reload_langgraph_agent", lambda: (False, "boom"))
    ok, _ = ai._apply_settings_changes()
    assert not ok
    assert bus_events == []
