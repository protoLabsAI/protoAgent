"""Runtime knobs + presets control surface (graph.knobs).

Generalizes the SpaceTraders engine's tunable-knobs + strategy-presets surface: typed knobs
read live, coerced/clamped/validated on set, named presets, a change log, and auto-generated
agent tools. Pure stdlib — tested directly; the tool factory needs langchain (a core dep).
"""

from __future__ import annotations

import pytest

from graph.knobs import Knobs, make_knob_tools
from graph.sdk import Knobs as SdkKnobs  # re-exported on the plugin SDK surface


def _knobs() -> Knobs:
    return (Knobs()
            .define("min_margin", 30, lo=0, help="cr/unit floor")
            .define("buy_buffer", 600_000, lo=0)
            .define("mining", True)
            .define("sink_cutoff", "ABUNDANT", choices=["LIMITED", "HIGH", "ABUNDANT"]))


def test_exported_on_the_sdk():
    assert SdkKnobs is Knobs


def test_define_infers_type_and_defaults():
    k = _knobs()
    assert k.get("min_margin") == 30
    assert k.get("mining") is True
    assert k.values() == {"min_margin": 30, "buy_buffer": 600_000,
                          "mining": True, "sink_cutoff": "ABUNDANT"}


def test_set_coerces_number_from_string():
    k = _knobs()
    assert "15" in k.set("min_margin", "15")
    assert k.get("min_margin") == 15 and isinstance(k.get("min_margin"), int)


def test_set_clamps_to_bounds():
    k = _knobs()
    k.set("min_margin", "-5")
    assert k.get("min_margin") == 0  # clamped to lo


def test_set_bool_from_truthy_strings():
    k = _knobs()
    k.set("mining", "off")
    assert k.get("mining") is False
    k.set("mining", "yes")
    assert k.get("mining") is True


def test_choices_are_validated_case_insensitively():
    k = _knobs()
    assert "high" in k.set("sink_cutoff", "high").lower()
    assert k.get("sink_cutoff") == "HIGH"  # normalized to the declared choice
    msg = k.set("sink_cutoff", "GALAXY")
    assert "must be one of" in msg
    assert k.get("sink_cutoff") == "HIGH"  # unchanged on bad value


def test_unknown_knob_is_a_readable_message_not_a_raise():
    k = _knobs()
    assert "unknown knob" in k.set("warp_speed", "9")


def test_changes_log_records_only_real_changes():
    k = _knobs()
    k.set("min_margin", "30")     # same value → no log entry
    k.set("min_margin", "20")     # changed → logged
    log = k.changes()
    assert len(log) == 1 and log[0]["action"] == "tune" and "30 → 20" in log[0]["detail"]


def test_presets_apply_as_a_bundle_and_are_not_cumulative():
    k = _knobs()
    k.preset("trade-max", {"buy_buffer": 300_000, "min_margin": 20}, blurb="pure arbitrage")
    k.set("min_margin", "99")
    k.apply_preset("trade-max")
    assert k.get("buy_buffer") == 300_000 and k.get("min_margin") == 20
    # a knob not in the preset resets to its default (not the cumulative 99)
    k.set("min_margin", "99")
    k.apply_preset("trade-max")
    assert k.get("min_margin") == 20
    assert any(c["action"] == "preset" for c in k.changes())


def test_preset_with_unknown_knob_is_rejected_at_declaration():
    with pytest.raises(ValueError):
        Knobs().define("a", 1).preset("p", {"b": 2})


def test_reset_restores_defaults():
    k = _knobs()
    k.set("min_margin", "5")
    k.set("sink_cutoff", "LIMITED")
    k.reset()
    assert k.get("min_margin") == 30 and k.get("sink_cutoff") == "ABUNDANT"


def test_schema_describes_choices_and_ranges():
    rows = {r["name"]: r for r in _knobs().schema()}
    assert rows["sink_cutoff"]["choices"] == ["LIMITED", "HIGH", "ABUNDANT"]
    assert rows["min_margin"]["range"] == [0, None]


# ── tool factory ───────────────────────────────────────────────────────────────────────
def test_make_knob_tools_generates_named_tools():
    k = _knobs().preset("trade-max", {"min_margin": 20})
    tools = make_knob_tools(k, prefix="fleet")
    names = {t.name for t in tools}
    assert names == {"fleet_knobs", "fleet_tune", "fleet_preset"}


def test_no_preset_tool_when_no_presets_declared():
    tools = make_knob_tools(_knobs(), prefix="fleet")
    assert {t.name for t in tools} == {"fleet_knobs", "fleet_tune"}


async def test_generated_tune_tool_sets_the_knob():
    k = _knobs()
    tune = {t.name: t for t in make_knob_tools(k, prefix="fleet")}["fleet_tune"]
    out = await tune.ainvoke({"knob": "min_margin", "value": "12"})
    assert "12" in out and k.get("min_margin") == 12


async def test_generated_preset_tool_lists_and_applies():
    k = _knobs().preset("trade-max", {"min_margin": 20}, blurb="pure arbitrage")
    preset = {t.name: t for t in make_knob_tools(k, prefix="fleet")}["fleet_preset"]
    listed = await preset.ainvoke({"name": ""})
    assert "trade-max" in listed
    await preset.ainvoke({"name": "trade-max"})
    assert k.get("min_margin") == 20
