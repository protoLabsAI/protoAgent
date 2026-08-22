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
