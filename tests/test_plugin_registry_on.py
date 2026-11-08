"""``registry.on()`` delivers events even when ``register()`` runs before the host bus is
wired, and the registry owns its unsubscribes so a reload doesn't stack handlers (#4800).

These model the REAL desktop ordering with the real :class:`PluginRegistry` and the real
:class:`EventBus`: subscribe first (bus not wired), then wire the bus, then publish — so no
testkit fake is needed. ``on()`` queues the subscription; a bind step delivers it once the
bus exists; a teardown step unsubscribes this load's handlers before a reload binds the next.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from events.bus import EventBus
from graph.plugins.host import PluginHost
from graph.plugins.registry import PluginRegistry


def _registry(host: PluginHost, plugin_id: str = "myplugin") -> PluginRegistry:
    """A real PluginRegistry whose injected host is ``host`` (so a test controls whether the
    event bus is wired), the same seam the server populates in production."""
    reg = PluginRegistry(plugin_id, Path("/tmp"))
    reg.host = host
    return reg


def test_on_before_bus_is_wired_then_binds_and_delivers_once():
    """register() ran before the bus existed (the desktop cold boot). The subscription waits
    in the queue; once the bus is wired and the bind step runs, the handler goes live and a
    published event reaches it exactly once."""
    bus = EventBus()
    host = PluginHost()  # on=None — no bus yet, exactly as on cold boot
    reg = _registry(host)

    got: list[dict] = []
    reg.on("topic.*", lambda payload: got.append(payload))

    # Queued, not yet delivered to any bus.
    assert bus.handler_count() == 0
    assert reg._bind_event_subscriptions() == 1  # still pending — no bus to bind to

    # The server wires the bus, then drains the queue.
    host.on = bus.subscribe_handler
    assert reg._bind_event_subscriptions() == 0  # bound, nothing left pending
    assert bus.handler_count() == 1

    bus.publish("topic.created", {"n": 1})
    assert len(got) == 1
    assert got[0]["event"] == "topic.created"
    assert got[0]["data"] == {"n": 1}

    # A non-matching topic is not delivered (the subscription pattern still applies).
    bus.publish("other.created", {"n": 2})
    assert len(got) == 1


def test_on_after_bus_is_wired_binds_immediately_and_delivers():
    """An on() made when the bus is ALREADY wired — the common after-load case: a surface
    start, a route handler, a lifecycle hook, a delayed callback over a captured registry, a
    hot-reload register() — binds RIGHT THEN, with no later host bind step, and a published
    event reaches it once. This is the case the queue must never silently swallow (#4800)."""
    bus = EventBus()
    host = PluginHost()
    host.on = bus.subscribe_handler  # the server is already up
    reg = _registry(host)

    got: list[dict] = []
    reg.on("topic.*", lambda payload: got.append(payload))

    # Bound by on() itself — nothing left queued and no host bind step ran.
    assert bus.handler_count() == 1
    assert reg._pending_subscriptions == []
    assert len(reg._subscription_unsubs) == 1

    bus.publish("topic.created", {"n": 1})
    assert len(got) == 1
    assert got[0]["event"] == "topic.created"

    # The registry still owns the unsubscribe, so teardown removes the live handler.
    reg._teardown_event_subscriptions()
    assert bus.handler_count() == 0


def test_reload_twice_leaves_exactly_one_live_handler():
    """A plugin reloaded keeps exactly one live handler: the outgoing load's subscription is
    torn down before the reloaded one binds, instead of stacking a second closed over stale
    state. Models reload → tear down old → build new → bind new."""
    bus = EventBus()
    host = PluginHost()
    host.on = bus.subscribe_handler  # server is up: the bus is already wired on a hot reload

    got: list[str] = []

    def _load(tag: str) -> PluginRegistry:
        reg = _registry(host)
        reg.on("topic.*", lambda _payload, _tag=tag: got.append(_tag))
        reg._bind_event_subscriptions()
        return reg

    reg1 = _load("load1")
    assert bus.handler_count() == 1

    # Reload #1: the server tears the previous load down, then binds the new one.
    reg1._teardown_event_subscriptions()
    reg2 = _load("load2")
    assert bus.handler_count() == 1

    # Reload #2: same again.
    reg2._teardown_event_subscriptions()
    _load("load3")
    assert bus.handler_count() == 1

    # Exactly one handler fires, and it is the newest load's — not three.
    bus.publish("topic.created", {})
    assert got == ["load3"]


def test_teardown_also_drops_a_still_queued_subscription():
    """Teardown clears the queue too, so a subscription that never reached the bus (register()
    ran, the bus never came up, the plugin is disabled) leaves nothing behind."""
    host = PluginHost()  # on=None — never wired
    reg = _registry(host)
    reg.on("topic.*", lambda _p: None)
    assert reg._has_event_subscriptions() is True

    reg._teardown_event_subscriptions()
    assert reg._has_event_subscriptions() is False
    # Wiring a bus and binding now has nothing to deliver.
    assert reg._bind_event_subscriptions() == 0


def test_unbound_subscription_after_load_warns_naming_plugin_and_topic(caplog):
    """A subscription still unbound after load completes in a server process (the bus was never
    wired) logs a WARNING naming the plugin id and topic."""
    host = PluginHost()  # on=None — the bus never came up
    reg = _registry(host, plugin_id="acmeplugin")
    reg.on("acmeplugin.ticket.*", lambda _p: None)

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        still_pending = reg._bind_event_subscriptions(warn_if_unbound=True)

    assert still_pending == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("acmeplugin" in r.getMessage() and "acmeplugin.ticket.*" in r.getMessage() for r in warnings), (
        f"expected a WARNING naming the plugin and topic, got: {[r.getMessage() for r in warnings]}"
    )


def test_no_bus_context_stays_quiet_without_warn_flag(caplog):
    """A non-server / test context with no bus is silent (the default): nothing to bind to by
    design, so queueing a subscription that never binds must not nag."""
    host = PluginHost()  # on=None
    reg = _registry(host)
    reg.on("topic.*", lambda _p: None)

    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        assert reg._bind_event_subscriptions() == 1  # warn_if_unbound defaults False

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_non_callable_handler_is_rejected_and_not_queued(caplog):
    """A non-callable handler is refused (the pre-existing guard) and never queued."""
    host = PluginHost()
    reg = _registry(host)
    with caplog.at_level(logging.WARNING, logger="protoagent.plugins"):
        reg.on("topic.*", "not-callable")
    assert reg._has_event_subscriptions() is False


# The server-side wiring the fix depends on (plugin_wiring helpers driven by
# _populate_plugin_host and the reload commit). A registry-only test passes even if these call
# sites are removed, so cover them directly: the cold-boot drain, and the reload teardown that
# keeps exactly one live handler (#4800).


def _bundle(*registries):
    """A minimal stand-in for the plugin bundle the loader hands the server — only the
    ``plugin_registries`` list the bind/teardown helpers read (the loader fills it with EVERY
    registry it produced, subscribing or not, so a first ``on()`` made after load is still
    reachable). The loader-backed tests below prove the real bundle is filled the same way."""
    from types import SimpleNamespace

    return SimpleNamespace(plugin_registries=list(registries))


def test_server_bind_helper_drains_queued_subscriptions_once_bus_is_wired():
    """The bind step _populate_plugin_host runs after wiring the bus delivers every subscribing
    registry's queued on() to the now-wired bus — the desktop cold-boot path. Removing this call
    site would leave cold-boot subscriptions queued forever."""
    from server.plugin_wiring import _bind_plugin_event_subscriptions

    bus = EventBus()
    host = PluginHost()  # on=None — register() ran before the bus came up
    reg = _registry(host)
    got: list[dict] = []
    reg.on("topic.*", lambda p: got.append(p))
    assert bus.handler_count() == 0  # still queued, nothing wired yet

    host.on = bus.subscribe_handler  # the server wires the bus...
    _bind_plugin_event_subscriptions(_bundle(reg))  # ...then drains the bundle's queues
    assert bus.handler_count() == 1

    bus.publish("topic.created", {})
    assert len(got) == 1


def test_server_reload_commit_tears_down_outgoing_then_binds_incoming():
    """The reload commit tears the OUTGOING bundle's handlers down before binding the incoming
    one, so a reload leaves exactly one live handler — the newest load, not a stack. Removing the
    teardown call site would strand the previous load's handler alongside the new one."""
    from server.plugin_wiring import (
        _bind_plugin_event_subscriptions,
        _teardown_plugin_event_subscriptions,
    )

    bus = EventBus()
    host = PluginHost()
    host.on = bus.subscribe_handler  # server is up: a hot reload, bus already wired
    got: list[str] = []

    def _load(tag: str) -> PluginRegistry:
        reg = _registry(host)
        reg.on("topic.*", lambda _p, _t=tag: got.append(_t))  # binds inline — bus is wired
        return reg

    old = _bundle(_load("load1"))
    _bind_plugin_event_subscriptions(old)  # idempotent: on() already bound it inline
    assert bus.handler_count() == 1

    new = _bundle(_load("load2"))  # reload builds the new load; it binds inline too
    _teardown_plugin_event_subscriptions(old)  # commit: drop the outgoing load...
    _bind_plugin_event_subscriptions(new)  # ...and bind the incoming (a no-op here)
    assert bus.handler_count() == 1

    bus.publish("topic.created", {})
    assert got == ["load2"]


# The loader-backed coverage the fix actually depends on: the real ``load_plugins`` carrying
# EVERY registry into ``bundle.plugin_registries`` (not just load-time subscribers), so the real
# ``_teardown_plugin_event_subscriptions`` reaches a plugin whose first ``on()`` comes after load
# — and the loader tearing down a handler bound inline by a ``register()`` that then raised. A
# registry-only test passes even if the loader carry-forward is reverted, so drive load_plugins.

_QUIET_PLUGIN = '''
def register(registry):
    """Loads cleanly and subscribes to nothing at register() time — its first on() comes later,
    from a surface start / route handler / lifecycle hook."""
'''

_RAISE_AFTER_ON_PLUGIN = '''
def register(registry):
    registry.on("boomplug.topic", lambda _p: None)  # binds inline when the bus is already wired
    raise RuntimeError("register() blew up right after subscribing")
'''


def _write_plugin(root: Path, pid: str, body: str) -> Path:
    """A minimal on-disk plugin (manifest + register()) under ``root`` — the same shape
    ``tests/test_plugin_reload_leak.py`` uses to drive the real loader."""
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: true\n", encoding="utf-8"
    )
    (d / "__init__.py").write_text(body, encoding="utf-8")
    return d


@pytest.fixture
def plugin_root(tmp_path, monkeypatch):
    """A plugins dir the real ``load_plugins`` discovers — and nothing else, so handler counts
    on a test bus reflect only the plugins written here."""
    from graph.plugins import loader as plugin_loader

    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [root])
    yield root
    # The re-exec cache is process state — don't leak these ids into the next test.
    for pid in ("quietplug", "latebird", "boomplug"):
        plugin_loader.purge_plugin_modules(pid)


def test_loader_carries_a_non_subscribing_registry_forward(plugin_root):
    """The loader carries EVERY registry into ``plugin_registries``, even one that subscribed to
    nothing at load. Reverting to the old ``_has_event_subscriptions()`` carry filter drops this
    registry — and then a reload can never tear down a handler it binds later (#4800)."""
    from graph.config import LangGraphConfig
    from graph.plugins.loader import load_plugins

    _write_plugin(plugin_root, "quietplug", _QUIET_PLUGIN)
    result = load_plugins(LangGraphConfig())

    assert "quietplug" in [r.plugin_id for r in result.plugin_registries]


def test_reload_teardown_reaches_a_registry_that_subscribed_after_load(plugin_root, monkeypatch):
    """The round-4 blocker, end to end on the REAL bundle + server teardown helper: a plugin
    whose first ``on()`` comes AFTER load (a surface start / route handler / lifecycle hook) must
    still be torn down on reload. The loader carries its registry forward; once the bus is up the
    late ``on()`` binds inline; ``_teardown_plugin_event_subscriptions`` reaches it (#4800)."""
    from graph.config import LangGraphConfig
    from graph.plugins import host as host_module
    from graph.plugins.loader import load_plugins
    from server.plugin_wiring import _teardown_plugin_event_subscriptions

    _write_plugin(plugin_root, "latebird", _QUIET_PLUGIN)
    result = load_plugins(LangGraphConfig())
    reg = next(r for r in result.plugin_registries if r.plugin_id == "latebird")

    # The bus comes up after load; the plugin's surface start subscribes now (binds inline,
    # because ``reg.host`` is the same HOST singleton we wire here).
    bus = EventBus()
    monkeypatch.setattr(host_module.HOST, "on", bus.subscribe_handler)
    reg.on("topic.*", lambda _p: None)
    assert bus.handler_count() == 1

    # A reload tears the OUTGOING bundle down — and it reaches this late subscriber.
    _teardown_plugin_event_subscriptions(result)
    assert bus.handler_count() == 0


def test_register_raising_after_on_does_not_leak_a_handler(plugin_root, monkeypatch):
    """A plugin whose ``register()`` calls ``on()`` (bus already wired, a hot reload) and then
    raises binds a handler inline but never finishes loading, so it is not carried forward and no
    later reload can reach it. The loader must unsubscribe it in its except branch, or it stays
    live on the bus for the life of the process (#4800)."""
    from graph.config import LangGraphConfig
    from graph.plugins import host as host_module
    from graph.plugins.loader import load_plugins

    _write_plugin(plugin_root, "boomplug", _RAISE_AFTER_ON_PLUGIN)

    bus = EventBus()
    monkeypatch.setattr(host_module.HOST, "on", bus.subscribe_handler)  # hot reload: bus is up
    result = load_plugins(LangGraphConfig())

    # It failed to load (register() raised) — not carried forward, flagged in meta...
    assert "boomplug" not in [r.plugin_id for r in result.plugin_registries]
    assert any(m.get("id") == "boomplug" and m.get("error") for m in result.meta)
    # ...and the handler it bound inline before raising was torn down, not left live.
    assert bus.handler_count() == 0
