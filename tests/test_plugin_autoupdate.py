"""Opt-in plugin auto-update loop (#1720).

Covers the sweep's gating (opt-in policy, pinned/behind, idle safe-moment) and the
pull+reload+event path — all with the installer/reload seams stubbed so no network
or FastAPI is touched. The loop reads ``plugins.update_policy`` +
``plugins.autoupdate_interval_hours`` off the live config each pass.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server import agent_init


def _cfg(**over):
    base = dict(
        plugins_enabled=["demo"],
        plugins_disabled=[],
        plugins_sources_allow=[],
        plugins_update_policy={"demo": {"track": "main", "when": "idle"}},
        plugins_autoupdate_interval_hours=6,
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def stub_installer(monkeypatch):
    """Stub the installer + reload + event seams; record what the sweep did."""
    calls = SimpleNamespace(installed=[], checked=[], reloaded=[], purged=[], events=[])

    entry = {
        "id": "demo",
        "source_url": "https://github.com/acme/demo",
        "requested_ref": "main",
        "resolved_sha": "a" * 40,
        "present": True,
    }

    def fake_list_installed():
        return [entry]

    def fake_check(e):
        return {"id": e["id"], "behind": True, "pinned": False, "latest_ref": None, "error": None}

    def fake_install(url, ref, *, force, by, allow):
        calls.installed.append((url, ref, force, by, allow))
        return {"version": "1.2.3", "resolved_sha": "b" * 40}

    def fake_is_release_tag(ref):
        return str(ref).startswith("v")

    def fake_purge(pid):
        calls.purged.append(pid)

    def fake_apply(config):
        calls.reloaded.append(config)
        return True, []

    import graph.plugins.installer as installer_mod
    import graph.plugins.loader as loader_mod

    monkeypatch.setattr(installer_mod, "list_installed", fake_list_installed)
    monkeypatch.setattr(installer_mod, "check_plugin_update", fake_check)
    monkeypatch.setattr(installer_mod, "install", fake_install)
    monkeypatch.setattr(installer_mod, "is_release_tag", fake_is_release_tag)
    monkeypatch.setattr(loader_mod, "purge_plugin_modules", fake_purge)
    monkeypatch.setattr(agent_init, "_apply_settings_changes", fake_apply)
    # Default: the server is idle so the safe-moment gate passes.
    monkeypatch.setattr(agent_init, "_server_is_idle", lambda: True)

    # Capture bus events without a real bus.
    import server as server_mod

    monkeypatch.setattr(server_mod._event_bus, "publish", lambda topic, payload: calls.events.append((topic, payload)))

    calls.entry = entry
    calls.installer = installer_mod
    return calls


async def test_sweep_updates_behind_enabled_plugin(stub_installer):
    cfg = _cfg()
    n = await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    assert n == 1
    # Pulled the branch head with force + the autoupdate actor.
    assert stub_installer.installed == [("https://github.com/acme/demo", "main", True, "autoupdate", None)]
    # Enabled → purged + reloaded through the enable path.
    assert stub_installer.purged == ["demo"]
    assert stub_installer.reloaded == [{"plugins": {"enabled": ["demo"], "disabled": []}}]
    # Emitted plugin.updated with the fresh sha + reloaded flag.
    assert len(stub_installer.events) == 1
    topic, payload = stub_installer.events[0]
    assert topic == "plugin.updated"
    assert payload["id"] == "demo" and payload["reloaded"] is True and payload["by"] == "autoupdate"
    assert payload["resolved_sha"] == "b" * 40


async def test_sweep_skips_when_not_behind(stub_installer, monkeypatch):
    monkeypatch.setattr(
        stub_installer.installer,
        "check_plugin_update",
        lambda e: {"behind": False, "pinned": False, "error": None},
    )
    cfg = _cfg()
    n = await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    assert n == 0
    assert stub_installer.installed == []


async def test_sweep_skips_pinned(stub_installer, monkeypatch):
    monkeypatch.setattr(
        stub_installer.installer,
        "check_plugin_update",
        lambda e: {"behind": True, "pinned": True, "error": None},
    )
    cfg = _cfg()
    n = await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    assert n == 0
    assert stub_installer.installed == []


async def test_sweep_skips_check_error(stub_installer, monkeypatch):
    monkeypatch.setattr(
        stub_installer.installer,
        "check_plugin_update",
        lambda e: {"behind": True, "pinned": False, "error": "ls-remote timed out"},
    )
    cfg = _cfg()
    assert await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy) == 0
    assert stub_installer.installed == []


async def test_sweep_requires_track(stub_installer):
    cfg = _cfg(plugins_update_policy={"demo": {"when": "idle"}})  # no track → not armed
    assert await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy) == 0
    assert stub_installer.installed == []


async def test_sweep_skips_uninstalled_policy_entry(stub_installer):
    cfg = _cfg(plugins_update_policy={"ghost": {"track": "main"}})
    assert await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy) == 0
    assert stub_installer.installed == []


async def test_idle_gate_defers_when_busy(stub_installer, monkeypatch):
    monkeypatch.setattr(agent_init, "_server_is_idle", lambda: False)
    cfg = _cfg()  # when: idle
    assert await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy) == 0
    assert stub_installer.installed == []


async def test_when_always_bypasses_idle_gate(stub_installer, monkeypatch):
    monkeypatch.setattr(agent_init, "_server_is_idle", lambda: False)
    cfg = _cfg(plugins_update_policy={"demo": {"track": "main", "when": "always"}})
    n = await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    assert n == 1
    assert len(stub_installer.installed) == 1


async def test_release_tag_targets_latest_ref(stub_installer, monkeypatch):
    stub_installer.entry["requested_ref"] = "v1.0.0"
    monkeypatch.setattr(
        stub_installer.installer,
        "check_plugin_update",
        lambda e: {"behind": True, "pinned": False, "latest_ref": "v1.1.0", "error": None},
    )
    cfg = _cfg()
    await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    # Immutable tag → install the newest tag, not the recorded one.
    assert stub_installer.installed[0][1] == "v1.1.0"


async def test_disabled_plugin_updates_without_reload(stub_installer):
    cfg = _cfg(plugins_enabled=[])  # installed-but-disabled
    n = await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    assert n == 1
    assert len(stub_installer.installed) == 1  # code still pulled
    assert stub_installer.purged == []  # nothing mounted → no purge/reload
    assert stub_installer.reloaded == []
    assert stub_installer.events[0][1]["reloaded"] is False


async def test_sources_allow_threaded_into_install(stub_installer):
    cfg = _cfg(plugins_sources_allow=["github.com/acme/*"])
    await agent_init._plugin_autoupdate_sweep(cfg, cfg.plugins_update_policy)
    assert stub_installer.installed[0][4] == ["github.com/acme/*"]


def test_server_is_idle_reads_beacon(monkeypatch):
    import importlib

    # ``server`` re-exports a ``chat`` function, shadowing the submodule as a
    # package attribute — import the real module explicitly.
    chat_mod = importlib.import_module("server.chat")

    monkeypatch.setattr(chat_mod, "seconds_since_last_turn", lambda: agent_init._AUTOUPDATE_IDLE_QUIET_S + 1)
    assert agent_init._server_is_idle() is True
    monkeypatch.setattr(chat_mod, "seconds_since_last_turn", lambda: 1.0)
    assert agent_init._server_is_idle() is False


def test_beacon_updates_on_activity(monkeypatch):
    import importlib

    chat_mod = importlib.import_module("server.chat")

    # Fresh process: no turn yet → infinitely idle.
    monkeypatch.setattr(chat_mod, "_LAST_TURN_MONOTONIC", 0.0)
    assert chat_mod.seconds_since_last_turn() == float("inf")
    # A turn stamps the beacon → recent activity.
    chat_mod._note_chat_activity()
    assert chat_mod.seconds_since_last_turn() < 5.0
