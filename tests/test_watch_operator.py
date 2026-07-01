"""Watch operator surface (/api/watches handlers) + sdk.create_watch + the register_watch_hook
plugin seam (ADR 0067 PR2)."""

import pytest

from graph.config import LangGraphConfig
from graph.watches.controller import WatchController
from graph.watches.store import WatchStore


def _wire(monkeypatch, tmp_path):
    from runtime.state import STATE

    ctrl = WatchController(LangGraphConfig(), WatchStore(tmp_path))
    monkeypatch.setattr(STATE, "watch_controller", ctrl)
    return ctrl


# --- operator /api/watches handlers ----------------------------------------


@pytest.mark.asyncio
async def test_operator_watches_set_accepts_any_verifier(monkeypatch, tmp_path):
    from operator_api import console_handlers

    _wire(monkeypatch, tmp_path)
    # A command verifier is allowed via the operator channel (trusted=True), unlike the
    # plugin-only agent/SDK path — safe because /api is operator-tier by the ADR 0066 ceiling.
    res = await console_handlers._operator_watches_set(
        {"condition": "tests pass", "verifier": {"type": "command", "command": "pytest -q"}}
    )
    assert res["ok"] is True


@pytest.mark.asyncio
async def test_operator_watches_list_and_clear(monkeypatch, tmp_path):
    from operator_api import console_handlers

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="watch a", verifier={"type": "plugin", "check": "p:v"})
    ctrl.create(condition="watch b", verifier={"type": "plugin", "check": "p:v"})
    listed = await console_handlers._operator_watches_list()
    assert listed["enabled"] is True and len(listed["watches"]) == 2
    wid = listed["watches"][0]["id"]
    assert (await console_handlers._operator_watches_clear(wid))["cleared"] is True


@pytest.mark.asyncio
async def test_operator_watches_disabled_when_no_controller(monkeypatch):
    from operator_api import console_handlers
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "watch_controller", None)
    assert (await console_handlers._operator_watches_list())["enabled"] is False
    assert (await console_handlers._operator_watches_set({"condition": "c"}))["ok"] is False


# --- sdk.create_watch (plugin-only) ----------------------------------------


def test_sdk_create_watch_registers_a_plugin_watch(monkeypatch, tmp_path):
    from graph import sdk

    _wire(monkeypatch, tmp_path)
    res = sdk.create_watch(condition="reach 1M", verifier="spacetraders:credits", verifier_args={"min": 1_000_000})
    assert res["ok"] is True and res["watch_id"]


def test_sdk_create_watch_unavailable(monkeypatch):
    from graph import sdk
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "watch_controller", None)
    assert sdk.create_watch(condition="c", verifier="p:v")["ok"] is False


def test_sdk_module_exposes_create_watch():
    from graph import sdk

    assert callable(sdk.create_watch)


# --- registry / loader register_watch_hook seam ----------------------------


def test_registry_register_watch_hook():
    from graph.plugins.registry import PluginRegistry

    reg = PluginRegistry.__new__(PluginRegistry)  # skip HOST import in __init__
    reg.plugin_id = "demo"
    reg.watch_hooks = []

    def on_met(w):
        return None

    reg.register_watch_hook(on_met=on_met)
    assert len(reg.watch_hooks) == 1 and reg.watch_hooks[0]["on_met"] is on_met
    reg.register_watch_hook()  # nothing callable → rejected, no append
    assert len(reg.watch_hooks) == 1


def test_loader_result_has_watch_hooks():
    from graph.plugins.loader import PluginLoadResult

    assert PluginLoadResult().watch_hooks == []
