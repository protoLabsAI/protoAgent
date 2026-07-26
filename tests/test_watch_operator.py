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



@pytest.mark.asyncio
async def test_operator_watches_update_patches_only_what_it_is_sent(monkeypatch, tmp_path):
    from operator_api import console_handlers

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="rollout lands", watch_id="w", verifier={"type": "plugin", "check": "p:v"},
                interval_s=60, stall_after=4)
    res = await console_handlers._operator_watches_update("w", {"interval_s": 1800})
    assert res["ok"] is True
    w = ctrl.store.get("w")
    assert w.interval_s == 1800
    assert w.stall_after == 4  # absent from the body → untouched (PATCH, not PUT)
    assert w.condition == "rollout lands"


@pytest.mark.asyncio
async def test_operator_watches_update_null_clears_a_field(monkeypatch, tmp_path):
    from operator_api import console_handlers

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="c", watch_id="w", verifier={"type": "plugin", "check": "p:v"},
                deadline=9_999_999_999)
    await console_handlers._operator_watches_update("w", {"deadline": None})
    assert ctrl.store.get("w").deadline is None


@pytest.mark.asyncio
async def test_operator_watches_update_parses_an_iso_deadline(monkeypatch, tmp_path):
    from operator_api import console_handlers

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="c", watch_id="w", verifier={"type": "plugin", "check": "p:v"})
    await console_handlers._operator_watches_update("w", {"deadline": "2030-01-01T00:00:00+00:00"})
    assert ctrl.store.get("w").deadline == 1893456000.0


@pytest.mark.asyncio
async def test_operator_watches_update_may_change_the_verifier(monkeypatch, tmp_path):
    # Trusted channel: the operator can re-aim a watch at a shell verifier, which the
    # agent/SDK path is denied.
    from operator_api import console_handlers

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="c", watch_id="w", verifier={"type": "plugin", "check": "p:v"})
    res = await console_handlers._operator_watches_update(
        "w", {"verifier": {"type": "command", "command": "exit 0"}}
    )
    assert res["ok"] is True and ctrl.store.get("w").verifier["type"] == "command"


@pytest.mark.asyncio
async def test_operator_watches_update_rejects_empty_and_unknown(monkeypatch, tmp_path):
    from operator_api import console_handlers

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="c", watch_id="w", verifier={"type": "plugin", "check": "p:v"})
    assert "nothing to update" in (await console_handlers._operator_watches_update("w", {}))["error"]
    # A watch can't be disarmed by emptying its verifier — it would never evaluate again.
    blank = await console_handlers._operator_watches_update("w", {"verifier": {}})
    assert "cannot be empty" in blank["error"]
    missing = await console_handlers._operator_watches_update("nope", {"interval_s": 5})
    assert missing["ok"] is False and "no watch" in missing["error"]


# --- sdk.update_watch ------------------------------------------------------


@pytest.mark.asyncio
async def test_sdk_update_watch_edits_in_place(monkeypatch, tmp_path):
    from graph import sdk

    ctrl = _wire(monkeypatch, tmp_path)
    ctrl.create(condition="c", watch_id="st-fuel", verifier={"type": "plugin", "check": "p:v"}, stall_after=2)
    res = await sdk.update_watch("st-fuel", stall_after=6)
    assert res["ok"] is True and res["watch_id"] == "st-fuel"
    assert ctrl.store.get("st-fuel").stall_after == 6


@pytest.mark.asyncio
async def test_sdk_update_watch_unavailable(monkeypatch):
    from graph import sdk
    from runtime.state import STATE

    monkeypatch.setattr(STATE, "watch_controller", None)
    assert (await sdk.update_watch("x", interval_s=5))["ok"] is False


def test_sdk_module_exposes_update_watch():
    from graph import sdk

    assert callable(sdk.update_watch)

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


# --- the verifier catalog (what a chooser can offer, ADR 0028/0067) --------


def test_verifier_catalog_lists_every_core_type_including_plugin(monkeypatch):
    """The console used to hardcode these in TypeScript and had silently dropped `plugin`,
    making every contributed check unreachable from the operator UI. The catalog is built
    from the LIVE registry so it can't drift again."""
    from graph.goals.verifiers import VERIFIERS, verifier_catalog

    cat = verifier_catalog()
    # Curated presentation order, not alphabetical — `llm` (the common default) reads last,
    # not buried mid-list. Every core type is present regardless.
    assert {t["value"] for t in cat["types"]} == set(VERIFIERS)
    assert [t["value"] for t in cat["types"]][:2] == ["command", "test"]
    assert [t["value"] for t in cat["types"]][-1] == "plugin"
    assert "plugin" in [t["value"] for t in cat["types"]]
    assert all(t["source"] == "core" for t in cat["types"])
    assert all(t["description"] for t in cat["types"])  # every type explains itself


def test_verifier_catalog_reports_registered_plugin_checks(monkeypatch):
    from graph.goals import verifiers as gv

    async def _fn(spec, ctx): ...

    monkeypatch.setattr(gv, "_PLUGIN_VERIFIERS", {"st:credits": _fn, "cc:new_matches": _fn})
    monkeypatch.setattr(
        gv, "_PLUGIN_VERIFIER_META", {"st:credits": {"plugin_id": "st", "description": "Credits ≥ args.min"}}
    )
    checks = gv.verifier_catalog()["plugin_checks"]

    assert [c["name"] for c in checks] == ["cc:new_matches", "st:credits"]  # sorted
    assert all(c["source"] == "plugin" for c in checks)
    by_name = {c["name"]: c for c in checks}
    assert by_name["st:credits"]["description"] == "Credits ≥ args.min"
    # Registered before `description` existed → still attributed, via the namespace.
    assert by_name["cc:new_matches"]["plugin_id"] == "cc"
    assert by_name["cc:new_matches"]["description"] == ""


def test_verifier_catalog_is_empty_of_plugins_when_none_registered(monkeypatch):
    # A chooser hides its `plugin` option in this case rather than offering an empty picker.
    from graph.goals import verifiers as gv

    monkeypatch.setattr(gv, "_PLUGIN_VERIFIERS", {})
    assert gv.verifier_catalog()["plugin_checks"] == []


def test_register_goal_verifier_records_a_description(tmp_path):
    """The SDK addition is additive — an older plugin passing (name, fn) still registers."""
    from graph.plugins.registry import PluginRegistry

    reg = PluginRegistry("demo", tmp_path)

    async def _fn(spec, ctx): ...

    reg.register_goal_verifier("checks_out", _fn, "Reads real state")
    reg.register_goal_verifier("legacy", _fn)  # no description — must still work
    assert "demo:checks_out" in reg.goal_verifiers
    assert reg.goal_verifier_meta["demo:checks_out"]["description"] == "Reads real state"
    assert reg.goal_verifier_meta["demo:legacy"]["description"] == ""
