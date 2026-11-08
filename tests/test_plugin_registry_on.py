"""Subscription ownership follows call phase, across real loader and server reloads (#4800)."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from events.bus import EventBus
from graph.config import LangGraphConfig
from graph.plugins import host as host_module
from graph.plugins import loader
from graph.plugins.host import PluginHost
from graph.plugins.registry import PluginRegistry
from server.plugin_wiring import _bind_plugin_event_subscriptions, _teardown_plugin_event_subscriptions

pytestmark = pytest.mark.platform_sensitive


@pytest.fixture
def plugin_env(tmp_path, monkeypatch):
    root = tmp_path / "plugins"
    plugin = root / "eventplug"
    plugin.mkdir(parents=True)
    (plugin / "protoagent.plugin.yaml").write_text(
        "id: eventplug\nname: Event plugin\nversion: 0.1.0\nenabled: true\n", encoding="utf-8"
    )
    (plugin / "__init__.py").write_text('''
def register(registry):
    registry.received = []
    registry.on_result = registry.on("topic.*", registry.received.append)
''', encoding="utf-8")
    monkeypatch.setattr(loader, "_plugin_roots", lambda config: [root])
    # A fresh host keeps _populate_plugin_host's assignments isolated too.
    host = PluginHost()
    monkeypatch.setattr(host_module, "HOST", host)
    bus = EventBus()
    yield SimpleNamespace(root=root, plugin=plugin, host=host, bus=bus)
    loader.purge_plugin_modules("eventplug")


def _load():
    bundle = loader.load_plugins(LangGraphConfig())
    assert len(bundle.plugin_registries) == 1, bundle.meta
    reg = bundle.plugin_registries[0]
    assert reg._in_register is False
    assert reg.on_result is None
    return bundle, reg


def test_cold_boot_register_then_populate_host_delivers_once(plugin_env, monkeypatch):
    from runtime.state import STATE
    from server import plugin_wiring

    bundle, reg = _load()
    assert plugin_env.bus.handler_count() == 0
    monkeypatch.setattr(STATE, "plugin_bundle", bundle)
    monkeypatch.setattr(plugin_wiring, "_event_bus", plugin_env.bus)
    plugin_wiring._populate_plugin_host()
    plugin_wiring._populate_plugin_host()  # repeat wiring never duplicates subscriptions
    plugin_env.bus.publish("topic.created", {"n": 1})
    plugin_env.bus.publish("other.created", {})
    assert len(reg.received) == 1
    assert reg.received[0]["data"] == {"n": 1}
    assert plugin_env.bus.handler_count() == 1


@pytest.fixture
def reload_server(plugin_env, tmp_path, monkeypatch):
    """Run the real reload transaction and loader with unrelated builders stubbed out."""
    import graph.config_io as cio
    import server.agent_init as ai
    from runtime.state import STATE
    from tools.lg_tools import set_disabled_tools

    # Restore every STATE field changed by the real commit, not just fields read by this test.
    for name, value in vars(STATE).copy().items():
        monkeypatch.setattr(STATE, name, value)
    leaf = tmp_path / "config.yaml"
    leaf.write_text("scheduler:\n  enabled: false\n", encoding="utf-8")
    monkeypatch.setattr(cio, "config_yaml_path", lambda: leaf)
    monkeypatch.setattr(cio, "ensure_live_config", lambda: None)
    monkeypatch.setattr(cio, "is_setup_complete", lambda: True)
    monkeypatch.setattr("graph.config.model_is_configured", lambda cfg: False)
    for name in (
        "_build_knowledge_store", "_build_history_store", "_build_inbox_store",
        "_build_memory_store", "_build_shared_profile_store",
        "_build_skills_index", "_register_plugin_subagents", "_apply_config_subagents",
        "_apply_plugin_registries", "_mount_plugin_routers", "_reload_plugin_surfaces",
        "_audit_persona_tools", "_audit_legacy_tool_references", "_close_mcp_clients",
    ):
        monkeypatch.setattr(ai, name, lambda *a, **kw: None)
    monkeypatch.setattr(ai, "_apply_plugin_knowledge_backend", lambda cfg, store, plugins: store)
    monkeypatch.setattr(ai, "_resolve_plugin_middleware", lambda *a: [])
    monkeypatch.setattr(ai, "_build_mcp", lambda *a, **kw: ([], [], []))
    monkeypatch.setattr(ai, "_build_plugins", lambda *a, **kw: _load()[0])
    monkeypatch.setattr("server.a2a.refresh_served_card", lambda: None)
    monkeypatch.setattr("security.egress.set_allowed_hosts", lambda *a, **kw: None)
    monkeypatch.setattr("security.policy.set_callback_allowlist", lambda *a, **kw: None)
    monkeypatch.setattr("security.public_console.set_public_console_url", lambda *a, **kw: None)
    for name in ("scheduler", "goal_controller", "workflow_registry", "workflow_run"):
        monkeypatch.setattr(STATE, name, None)
    monkeypatch.setattr(STATE, "mcp_clients", [])
    plugin_env.host.on = plugin_env.bus.subscribe_handler
    bundle, reg = _load()
    _bind_plugin_event_subscriptions(bundle)
    monkeypatch.setattr(STATE, "plugin_bundle", bundle)
    yield SimpleNamespace(ai=ai, state=STATE, old=reg, bundle=bundle)
    set_disabled_tools([])


def test_full_reload_twice_no_incoming_delivery_mid_build(plugin_env, reload_server, monkeypatch):
    server = reload_server
    old = server.old
    for _ in range(2):
        incoming = []

        def build(*args, **kwargs):
            bundle, reg = _load()
            incoming.append(reg)
            before = len(old.received)
            plugin_env.bus.publish("topic.midbuild", {})
            assert len(old.received) == before + 1
            assert reg.received == []
            assert plugin_env.bus.handler_count() == 1
            return bundle

        monkeypatch.setattr(server.ai, "_build_plugins", build)
        ok, message = server.ai._reload_langgraph_agent()
        assert ok, message
        reg = incoming[0]
        before = len(old.received)
        plugin_env.bus.publish("topic.committed", {})
        assert len(old.received) == before
        assert len(reg.received) == 1
        assert plugin_env.bus.handler_count() == 1
        old = reg


def test_failed_reload_after_plugin_build_binds_nothing(plugin_env, reload_server, monkeypatch):
    server = reload_server
    incoming = []

    def build(*args, **kwargs):
        bundle, reg = _load()
        incoming.append(reg)
        return bundle

    def fail(*args, **kwargs):
        plugin_env.bus.publish("topic.failedbuild", {})
        raise RuntimeError("failure after _build_plugins")

    monkeypatch.setattr(server.ai, "_build_plugins", build)
    monkeypatch.setattr(server.ai, "_build_skills_index", fail)
    ok, message = server.ai._reload_langgraph_agent()
    assert not ok and "failure after _build_plugins" in message
    assert server.state.plugin_bundle is server.bundle
    assert incoming[0].received == []
    assert incoming[0]._subscription_unsubs == []
    assert plugin_env.bus.handler_count() == 1
    plugin_env.bus.publish("topic.afterfailure", {})
    assert len(server.old.received) == 2
    assert incoming[0].received == []


def test_post_load_caller_subscription_survives_full_reload(plugin_env, reload_server):
    received = []
    unsubscribe = reload_server.old.on("topic.*", received.append)
    assert callable(unsubscribe)
    assert plugin_env.bus.handler_count() == 2
    plugin_env.bus.publish("topic.before", {})
    assert len(received) == 1
    ok, message = reload_server.ai._reload_langgraph_agent()
    assert ok, message
    plugin_env.bus.publish("topic.after", {})
    assert len(received) == 2
    assert plugin_env.bus.handler_count() == 2
    unsubscribe()
    plugin_env.bus.publish("topic.stopped", {})
    assert len(received) == 2
    assert plugin_env.bus.handler_count() == 1


def test_disable_removes_only_bundle_owned_handlers(plugin_env, reload_server, monkeypatch):
    received = []
    unsubscribe = reload_server.old.on("topic.*", received.append)
    monkeypatch.setattr(reload_server.ai, "_build_plugins", lambda *a, **kw: loader.PluginLoadResult())
    ok, message = reload_server.ai._reload_langgraph_agent()
    assert ok, message
    plugin_env.bus.publish("topic.disabled", {})
    assert reload_server.old.received == []
    assert len(received) == 1
    unsubscribe()
    assert plugin_env.bus.handler_count() == 0


def test_register_raises_drops_queue_without_ever_binding(plugin_env, monkeypatch):
    captured = []
    original_init = PluginRegistry.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        captured.append(self)

    monkeypatch.setattr(PluginRegistry, "__init__", capture)
    (plugin_env.plugin / "__init__.py").write_text('''
def register(registry):
    registry.on("topic.*", lambda payload: None)
    registry.host.publish("topic.duringregister", {})
    raise RuntimeError("register failed")
''', encoding="utf-8")
    binds = []
    plugin_env.host.on = lambda *args: binds.append(args)
    plugin_env.host.publish = plugin_env.bus.publish
    result = loader.load_plugins(LangGraphConfig())
    assert result.plugin_registries == []
    assert any("register failed" in m.get("error", "") for m in result.meta)
    assert binds == []
    assert captured[0]._pending_subscriptions == []
    assert captured[0]._in_register is False


def test_server_bind_warns_with_plugin_and_topic(plugin_env, caplog):
    bundle, reg = _load()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        _bind_plugin_event_subscriptions(bundle, warn_if_unbound=True)
    assert any(
        r.levelno == logging.WARNING and "eventplug" in r.message and "topic.*" in r.message
        for r in caplog.records
    )
    assert len(reg._pending_subscriptions) == 1


def test_post_load_before_wiring_queues_and_is_bundle_owned():
    host = PluginHost()
    reg = PluginRegistry("late", Path("."))
    reg.host = host
    received = []
    assert reg.on("topic.*", received.append) is None
    assert reg._bind_event_subscriptions() == 1
    bus = EventBus()
    host.on = bus.subscribe_handler
    bundle = SimpleNamespace(plugin_registries=[reg])
    _bind_plugin_event_subscriptions(bundle)
    bus.publish("topic.bound", {})
    assert len(received) == 1
    _teardown_plugin_event_subscriptions(bundle)
    bus.publish("topic.stopped", {})
    assert len(received) == 1
    assert bus.handler_count() == 0


def test_non_callable_handler_is_rejected(caplog):
    reg = PluginRegistry("invalid", Path("."))
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        assert reg.on("topic.*", "invalid") is None
    assert not reg._has_event_subscriptions()
    assert "needs a callable handler" in caplog.text


def test_prompt_only_reload_keeps_existing_bundle(plugin_env, reload_server):
    ok, message = reload_server.ai._reload_langgraph_agent(reload_plugins=False)
    assert ok, message
    assert reload_server.state.plugin_bundle is reload_server.bundle
    plugin_env.bus.publish("topic.reused", {})
    assert len(reload_server.old.received) == 1
    assert plugin_env.bus.handler_count() == 1


def test_non_subscribing_registry_is_retained_for_later_queue(plugin_env):
    (plugin_env.plugin / "__init__.py").write_text("def register(registry): pass\n", encoding="utf-8")
    bundle = loader.load_plugins(LangGraphConfig())
    assert len(bundle.plugin_registries) == 1
    received = []
    reg = bundle.plugin_registries[0]
    assert reg.on("topic.*", received.append) is None
    plugin_env.host.on = plugin_env.bus.subscribe_handler
    _bind_plugin_event_subscriptions(bundle)
    plugin_env.bus.publish("topic.late", {})
    assert len(received) == 1


def test_unwired_context_stays_quiet_and_teardown_drops_queue(plugin_env, caplog):
    bundle, reg = _load()
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        _bind_plugin_event_subscriptions(bundle)
    assert not [record for record in caplog.records if record.levelno >= logging.WARNING]
    _teardown_plugin_event_subscriptions(bundle)
    assert not reg._has_event_subscriptions()
    plugin_env.host.on = plugin_env.bus.subscribe_handler
    _bind_plugin_event_subscriptions(bundle)
    assert plugin_env.bus.handler_count() == 0
