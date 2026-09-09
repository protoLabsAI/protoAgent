"""The plugin setup-gap seam: a plugin that can't do its job says so where the
operator looks (``/api/runtime/status`` warnings), and the notice self-clears."""

from __future__ import annotations

import pytest

from graph.plugins import setup_gaps
from graph.plugins.registry import PluginRegistry


@pytest.fixture(autouse=True)
def _clean():
    setup_gaps.reset()
    yield
    setup_gaps.reset()


def test_report_set_clear_and_banner_text():
    setup_gaps.report("project_board", "br", "beads CLI 'br' not found", label="Project Board")
    setup_gaps.report("github", "auth", "gh is not authenticated")
    assert setup_gaps.warnings() == [
        "github: gh is not authenticated",
        "Project Board: beads CLI 'br' not found",
    ]
    setup_gaps.report("project_board", "br", None)  # recovered
    assert [g["key"] for g in setup_gaps.active()] == ["auth"]
    setup_gaps.report("github", "auth", "   ")  # blank clears too
    assert setup_gaps.active() == []
    setup_gaps.report("", "x", "ignored")  # no plugin id → no-op
    assert setup_gaps.active() == []


def test_registry_method_uses_display_name_and_clear_plugin(tmp_path):
    reg = PluginRegistry("project_board", tmp_path)
    reg.display_name = "Project Board"
    reg.report_setup_gap("coder", "no coder configured")
    reg.report_setup_gap("br", "br missing")
    assert setup_gaps.warnings() == ["Project Board: br missing", "Project Board: no coder configured"]
    setup_gaps.clear_plugin("project_board")  # the loader's disable hook
    assert setup_gaps.active() == []


def test_retain_drops_gaps_from_plugins_no_longer_present():
    setup_gaps.report("gone", "br", "x")
    setup_gaps.report("kept", "br", "y")
    setup_gaps.retain({"kept"})
    assert [g["plugin"] for g in setup_gaps.active()] == ["kept"]


def test_caps_message_length_and_gaps_per_plugin():
    setup_gaps.report("p", "k", "x" * 1000)
    assert len(setup_gaps.active()[0]["message"]) == setup_gaps.MAX_MESSAGE_CHARS
    for i in range(setup_gaps.MAX_GAPS_PER_PLUGIN + 5):
        setup_gaps.report("p", f"ts-{i}", "flood")
    assert sum(1 for g in setup_gaps.active() if g["plugin"] == "p") == setup_gaps.MAX_GAPS_PER_PLUGIN
    setup_gaps.report("p", "k", "updated")  # updating an existing key is always allowed
    assert any(g["message"] == "updated" for g in setup_gaps.active())


# ── Declarative actions (foundation for a future console mapper) ─────────────────────────


def test_old_signature_stores_no_actions_key_and_is_byte_identical():
    # r1: a legacy call must store EXACTLY the old shape — no empty "actions" key sneaks in.
    setup_gaps.report("p", "k", "msg")
    assert setup_gaps.active() == [{"plugin": "p", "label": "p", "key": "k", "message": "msg"}]
    assert setup_gaps.warnings() == ["p: msg"]


def test_plugin_config_action_is_scoped_to_the_reporting_plugin():
    # r2: plugin_config always targets THIS plugin — a caller can't aim the fix elsewhere.
    setup_gaps.report(
        "project_board",
        "coder",
        "no coder configured",
        action={"kind": "plugin_config", "label": "Configure delegate", "target": "some_other_plugin"},
    )
    assert setup_gaps.active()[0]["actions"] == [
        {"kind": "plugin_config", "target": "project_board", "label": "Configure delegate"}
    ]


def test_global_settings_action_keeps_a_safe_slug_target_and_fields():
    setup_gaps.report(
        "p",
        "k",
        "m",
        action={"kind": "global_settings", "target": "security.auth", "fields": ["token", "mode", 5, ""]},
    )
    assert setup_gaps.active()[0]["actions"] == [
        {"kind": "global_settings", "target": "security.auth", "fields": ["token", "mode"]}
    ]


def test_a_list_of_actions_is_kept_in_order():
    setup_gaps.report("p", "k", "m", action=[{"kind": "plugin_config"}, {"kind": "global_settings"}])
    assert [a["kind"] for a in setup_gaps.active()[0]["actions"]] == ["plugin_config", "global_settings"]


def test_unknown_action_kind_is_dropped():
    # r2: an unrecognized (potentially executable) kind is never retained.
    setup_gaps.report("p", "k", "m", action={"kind": "run_shell", "cmd": "rm -rf /"})
    assert "actions" not in setup_gaps.active()[0]


def test_executable_callback_field_is_not_retained():
    # r2: only allowlisted keys are copied, so a callback slipped in is dropped by construction.
    setup_gaps.report("p", "k", "m", action={"kind": "plugin_config", "on_click": lambda: None})
    action = setup_gaps.active()[0]["actions"][0]
    assert action == {"kind": "plugin_config", "target": "p"}
    assert "on_click" not in action


def test_arbitrary_url_target_is_rejected():
    # r2: no scheme://host can survive — the target must be a plain identifier or be dropped.
    setup_gaps.report("p", "k", "m", action={"kind": "global_settings", "target": "https://evil.example/steal"})
    assert setup_gaps.active()[0]["actions"] == [{"kind": "global_settings"}]


def test_html_in_label_is_stripped():
    # r2: a plugin string never carries markup through.
    setup_gaps.report("p", "k", "m", action={"kind": "plugin_config", "label": "<script>alert(1)</script>Fix"})
    label = setup_gaps.active()[0]["actions"][0]["label"]
    assert "<" not in label and ">" not in label
    assert label == "scriptalert(1)/scriptFix"


def test_action_bounds_cap_count_label_length_and_field_count():
    # r3: oversized input is bounded, not stored whole.
    setup_gaps.report("p", "many", "m", action=[{"kind": "plugin_config"}] * (setup_gaps.MAX_ACTIONS + 3))
    assert len(setup_gaps.active()[0]["actions"]) == setup_gaps.MAX_ACTIONS

    setup_gaps.report("p", "label", "m", action={"kind": "plugin_config", "label": "x" * 500})
    long = next(g for g in setup_gaps.active() if g["key"] == "label")["actions"][0]["label"]
    assert len(long) == setup_gaps.MAX_ACTION_STR_CHARS

    setup_gaps.report(
        "p",
        "fields",
        "m",
        action={"kind": "global_settings", "fields": [f"f{i}" for i in range(setup_gaps.MAX_ACTION_FIELDS + 5)]},
    )
    fields = next(g for g in setup_gaps.active() if g["key"] == "fields")["actions"][0]["fields"]
    assert len(fields) == setup_gaps.MAX_ACTION_FIELDS


def test_malformed_action_input_degrades_without_raising():
    # r3: hostile / nonsense payloads must not crash reporting (i.e. plugin loading).
    for bad in (123, "a string", ["not-a-dict"], {"kind": None}, {"no_kind": 1}, object(), {"kind": 42}):
        setup_gaps.report("p", "k", "m", action=bad)  # must not raise
        assert "actions" not in setup_gaps.active()[0]


def test_actions_are_removed_atomically_on_every_clear_path():
    # r4: clear (message=None), clear_plugin, and retain each drop action metadata with the gap.
    setup_gaps.report("p", "k", "m", action={"kind": "plugin_config"})
    assert "actions" in setup_gaps.active()[0]
    setup_gaps.report("p", "k", None)  # normal clear
    assert setup_gaps.active() == []

    setup_gaps.report("p", "k", "m", action={"kind": "plugin_config"})
    setup_gaps.clear_plugin("p")
    assert setup_gaps.active() == []

    setup_gaps.report("gone", "k", "m", action={"kind": "plugin_config"})
    setup_gaps.report("kept", "k2", "m2")
    setup_gaps.retain({"kept"})
    remaining = setup_gaps.active()
    assert [g["plugin"] for g in remaining] == ["kept"]
    assert "actions" not in remaining[0]


def test_active_returns_a_defensive_copy_of_actions():
    setup_gaps.report("p", "k", "m", action={"kind": "global_settings", "fields": ["a"]})
    view = setup_gaps.active()
    view[0]["actions"][0]["fields"].append("hacked")
    view[0]["actions"].append({"kind": "plugin_config"})
    assert setup_gaps.active()[0]["actions"] == [{"kind": "global_settings", "fields": ["a"]}]


def test_registry_method_forwards_action_and_scopes_it(tmp_path):
    reg = PluginRegistry("project_board", tmp_path)
    reg.display_name = "Project Board"
    reg.report_setup_gap("coder", "no coder configured", action={"kind": "plugin_config", "label": "Add a delegate"})
    assert setup_gaps.active()[0]["actions"] == [
        {"kind": "plugin_config", "target": "project_board", "label": "Add a delegate"}
    ]


def test_registry_old_signature_still_stores_no_actions(tmp_path):
    reg = PluginRegistry("project_board", tmp_path)
    reg.display_name = "Project Board"
    reg.report_setup_gap("br", "br missing")  # no action kwarg — the pre-existing call shape
    assert setup_gaps.active() == [
        {"plugin": "project_board", "label": "Project Board", "key": "br", "message": "br missing"}
    ]
