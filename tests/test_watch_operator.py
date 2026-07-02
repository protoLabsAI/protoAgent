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


# --- sdk.list_watches / sdk.clear_watch (#1638) -----------------------------


def test_sdk_list_watches_returns_id_condition_status_verifier(monkeypatch, tmp_path):
    from graph import sdk

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(
        condition="credits over 1M",
        verifier={"type": "plugin", "check": "st:credits", "args": {"min": 1_000_000}},
        watch_id="st-credits",
    )
    listed = sdk.list_watches()
    assert listed == [
        {
            "id": "st-credits",
            "condition": "credits over 1M",
            "status": "active",
            "verifier": {"type": "plugin", "check": "st:credits", "args": {"min": 1_000_000}},
        }
    ]
    # The returned verifier is a DEEP copy — mutating it (even the nested args) must not
    # corrupt the stored watch.
    listed[0]["verifier"]["check"] = "tampered"
    listed[0]["verifier"]["args"]["min"] = 0
    assert ctrl.store.get("st-credits").verifier["check"] == "st:credits"
    assert ctrl.store.get("st-credits").verifier["args"]["min"] == 1_000_000


def test_sdk_list_watches_prefix_filters_to_the_plugins_suite(monkeypatch, tmp_path):
    from graph import sdk

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="a", verifier={"type": "plugin", "check": "st:v"}, watch_id="st-a")
    ctrl.create(condition="b", verifier={"type": "plugin", "check": "st:v"}, watch_id="st-b")
    ctrl.create(condition="c", verifier={"type": "plugin", "check": "other:v"}, watch_id="other-c")
    assert {w["id"] for w in sdk.list_watches("st-")} == {"st-a", "st-b"}
    assert len(sdk.list_watches()) == 3  # no prefix → everything


def test_sdk_clear_watch_removes_and_reports_existence(monkeypatch, tmp_path):
    from graph import sdk

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="a", verifier={"type": "plugin", "check": "st:v"}, watch_id="st-a")
    assert sdk.clear_watch("st-a") is True
    assert sdk.list_watches() == []  # gone — no longer polled
    assert sdk.clear_watch("st-a") is False  # already gone
    assert sdk.clear_watch("never-existed") is False


def test_sdk_watch_reconcile_pattern(monkeypatch, tmp_path):
    """The #1638 payoff: arm_all() as reconcile — clear suite ids not in the current
    spec set, then create/replace the rest (heals a renamed/dropped spec)."""
    from graph import sdk

    ctrl = _wire(monkeypatch, tmp_path)
    # v1 armed two watches; v2 renamed st-opportunity → st-market.
    ctrl.create(condition="credits", verifier={"type": "plugin", "check": "st:v"}, watch_id="st-credits")
    ctrl.create(condition="opportunity", verifier={"type": "plugin", "check": "st:v"}, watch_id="st-opportunity")
    current_spec = {"st-credits", "st-market"}
    for watch in sdk.list_watches("st-"):
        if watch["id"] not in current_spec:
            assert sdk.clear_watch(watch["id"]) is True
    for wid in current_spec:
        sdk.create_watch(condition=f"cond {wid}", verifier="st:v", watch_id=wid)
    assert {w["id"] for w in sdk.list_watches("st-")} == current_spec


def test_sdk_list_and_clear_watch_unavailable(monkeypatch):
    from graph import sdk
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "watch_controller", None)
    assert sdk.list_watches() == []
    assert sdk.clear_watch("anything") is False


def test_sdk_module_exposes_watch_lifecycle():
    from graph import sdk

    assert callable(sdk.list_watches)
    assert callable(sdk.clear_watch)


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
