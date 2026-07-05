"""System lifecycle hooks + dispatch (ADR 0074, extends ADR 0039).

Mirrors tests/test_goal_hooks.py: the module-level hook registry fires the right
callback per event, a raising hook is swallowed, the registry guard, the config-driven
reaction path (prompt + webhook), the pure agent.active debounce, and the bus broadcast.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from graph.lifecycle import (
    IDLE_THRESHOLD_S,
    TOPICS,
    describe,
    fire,
    should_emit_active,
)
from graph.lifecycle import dispatch as lc_dispatch
from graph.lifecycle.hooks import fire_lifecycle_hook, set_lifecycle_hooks
from graph.plugins.registry import PluginRegistry


@pytest.mark.asyncio
async def test_fire_routes_by_event():
    fired: list[str] = []
    set_lifecycle_hooks(
        [
            {
                "plugin_id": "p",
                "on_app_loaded": lambda pl: fired.append("loaded"),
                "on_agent_active": lambda pl: fired.append("active"),
                "on_system_wake": lambda pl: fired.append("wake"),
            }
        ]
    )
    try:
        await fire_lifecycle_hook("app_loaded", {})
        await fire_lifecycle_hook("agent_active", {})
        await fire_lifecycle_hook("system_wake", {})
        await fire_lifecycle_hook("bogus_event", {})  # unknown → no-op
        assert fired == ["loaded", "active", "wake"]
    finally:
        set_lifecycle_hooks([])


@pytest.mark.asyncio
async def test_async_hook_runs_and_a_raising_hook_is_swallowed():
    seen: list[str] = []

    async def _ok(payload):
        seen.append("ok")

    def _boom(payload):
        raise RuntimeError("kaboom")

    set_lifecycle_hooks(
        [
            {"plugin_id": "q", "on_app_loaded": _boom, "on_agent_active": None, "on_system_wake": None},
            {"plugin_id": "p", "on_app_loaded": _ok, "on_agent_active": None, "on_system_wake": None},
        ]
    )
    try:
        await fire_lifecycle_hook("app_loaded", {})  # must not raise
        assert seen == ["ok"]
    finally:
        set_lifecycle_hooks([])


def test_registry_register_lifecycle_hook_guards():
    reg = PluginRegistry("p", Path("."))
    reg.register_lifecycle_hook(on_app_loaded=lambda pl: None)
    reg.register_lifecycle_hook()  # no callables → ignored
    reg.register_lifecycle_hook(on_agent_active="nope")  # non-callable → ignored
    assert len(reg.lifecycle_hooks) == 1
    hook = reg.lifecycle_hooks[0]
    assert hook["plugin_id"] == "p"
    assert callable(hook["on_app_loaded"])
    assert hook["on_agent_active"] is None and hook["on_system_wake"] is None


def test_debounce_emits_when_idle_and_suppresses_when_busy():
    # First turn since boot → always emit, previous_state "boot".
    emit, idle, prev = should_emit_active(1000.0, None)
    assert emit is True and idle == 0.0 and prev == "boot"

    # A turn after a long idle gap → emit, previous_state "idle".
    emit, idle, prev = should_emit_active(1000.0 + IDLE_THRESHOLD_S + 5, 1000.0)
    assert emit is True and idle == IDLE_THRESHOLD_S + 5 and prev == "idle"

    # A turn during a busy session (gap < threshold) → suppressed.
    emit, idle, prev = should_emit_active(1010.0, 1000.0)
    assert emit is False and idle == 10.0 and prev == "idle"


@pytest.mark.asyncio
async def test_config_reactions_dispatch(monkeypatch):
    """A configured lifecycle_hooks entry enqueues a prompt (run_in_session) and POSTs a
    webhook — both captured via monkeypatch, both isolated from the real subsystems."""
    enqueued: list[tuple[str, str]] = []
    posted: list[tuple[str, str]] = []

    def _fake_run_in_session(session_id, prompt, **kwargs):
        enqueued.append((session_id, prompt))
        return {"ok": True}

    async def _fake_post(url, event, payload):
        posted.append((url, event))

    # run_in_session is imported lazily from graph.sdk inside _run_reaction.
    monkeypatch.setattr("graph.sdk.run_in_session", _fake_run_in_session, raising=True)
    monkeypatch.setattr(lc_dispatch, "_post_webhook", _fake_post)

    # config() reads STATE.graph_config; give it an app_loaded reaction with a session
    # (app.loaded carries no session, so it must be configured) + a webhook.
    from runtime.state import STATE

    prev_cfg = STATE.graph_config
    STATE.graph_config = types.SimpleNamespace(
        lifecycle_hooks=[
            {"event": "app_loaded", "prompt": "review the boot", "session": "ops"},
            {"event": "app_loaded", "webhook": "https://example.test/hook"},
            {"event": "agent_active", "prompt": "ignored — different event"},
        ]
    )
    try:
        await fire("app_loaded", {"ts": 1.0, "previous_state": "boot"})
    finally:
        STATE.graph_config = prev_cfg

    assert enqueued == [("ops", "review the boot")]
    assert posted == [("https://example.test/hook", "app_loaded")]


@pytest.mark.asyncio
async def test_webhook_reaction_respects_egress_guard(monkeypatch):
    """A lifecycle webhook runs through security.egress (like fetch_url / the operator
    api_base): a link-local / cloud-metadata host is skipped (SSRF defense), a normal host
    is POSTed. Guards against a compromised/typo'd config steering a hook at 169.254.169.254."""
    posted: list[str] = []

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kwargs):
            posted.append(url)

    monkeypatch.setattr("httpx.AsyncClient", _FakeClient)

    # No allowlist ⇒ exercise the default SSRF-guard branch deterministically.
    from security import egress

    saved = egress.allowed_hosts()
    egress.set_allowed_hosts([])
    try:
        # Link-local / cloud-metadata — blocked even with allow_private=True → never POSTed.
        await lc_dispatch._post_webhook("http://169.254.169.254/hook", "app_loaded", {"ts": 1.0})
        assert posted == []
        # A normal (unresolvable-in-CI) host passes (block_unresolvable=False) → POSTed.
        await lc_dispatch._post_webhook("https://hooks.example.test/x", "system_wake", {"ts": 1.0})
        assert posted == ["https://hooks.example.test/x"]
    finally:
        egress.set_allowed_hosts(saved)


@pytest.mark.asyncio
async def test_fire_emits_lifecycle_event_on_the_bus(monkeypatch):
    """fire() broadcasts the dot-namespaced topic on the event bus (ADR 0039) so ANY
    plugin/console can react — no lifecycle_hook required. Mirrors the goal-bus test."""
    from graph.plugins.host import HOST
    from runtime.state import STATE

    events: list[tuple[str, dict]] = []
    orig = HOST.publish
    HOST.publish = lambda topic, data: events.append((topic, data))
    prev_cfg = STATE.graph_config
    STATE.graph_config = None  # no config reactions — isolate the bus broadcast
    try:
        payload = {"ts": 42.0, "agent": "protoagent", "previous_state": "boot"}
        await fire("app_loaded", payload)
    finally:
        HOST.publish = orig
        STATE.graph_config = prev_cfg

    assert events, "no event broadcast on the bus"
    topic, data = events[0]
    assert topic == "app.loaded" == TOPICS["app_loaded"]
    assert data["previous_state"] == "boot" and data["ts"] == 42.0


def test_describe_lists_the_three_events():
    text = describe()
    for topic in ("app.loaded", "agent.active", "system.wake"):
        assert topic in text
