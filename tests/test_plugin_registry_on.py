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
