"""Host-mediated read channel for frame components (ADR 0118 D5 amendment, #4143).

``register_component(name, validator, frame=..., reads=[...])`` declares which of the plugin's
OWN bearer-gated GET routes (relative to ``/api/plugins/<id>/``) the console host may fetch on
the frame's behalf. Covers the registry's log-and-skip validation (valid, escaping, ``..``,
query, ``**``, no frame), the testkit ``FakeRegistry`` raising on the same refusals, and the
loader + ``GET /api/components`` carrying ``reads`` through.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from graph import components
from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins
from graph.plugins.registry import PluginRegistry
from graph.plugins.testkit import FakeRegistry

PUBLIC = ["/plugins/p/component.html"]


def _v(props):
    return None


def _reg() -> PluginRegistry:
    return PluginRegistry("p", Path("/tmp"), public_paths=PUBLIC)


@pytest.fixture(autouse=True)
def _reset_component_state():
    yield
    components.set_plugin_components(None)
    components.set_plugin_component_frames(None)


# ── PluginRegistry: accept + log-and-skip ────────────────────────────────────────────────


def test_valid_reads_are_accepted_and_stored():
    reg = _reg()
    reg.register_component("thing-ref", _v, frame="component.html", reads=["features", "features/*", "status"])
    assert reg.components == {"thing-ref": _v}
    assert reg.component_reads == {"thing-ref": ["features", "features/*", "status"]}


def test_an_absolute_pattern_under_the_own_prefix_is_stripped_to_relative():
    reg = _reg()
    reg.register_component("thing-ref", _v, frame="component.html", reads=["/api/plugins/p/features/*"])
    assert reg.component_reads == {"thing-ref": ["features/*"]}


def test_duplicate_patterns_are_collapsed():
    reg = _reg()
    reg.register_component("thing-ref", _v, frame="component.html", reads=["status", "status"])
    assert reg.component_reads == {"thing-ref": ["status"]}


@pytest.mark.parametrize(
    "pattern",
    [
        "/api/plugins/other/features",  # escapes: another plugin's prefix
        "/api/config",  # escapes: core /api
        "/plugins/p/component.html",  # escapes: the public view router, not the data router
        "../other/features",  # parent traversal
        "features/../../config",  # traversal mid-path
        "features?limit=5",  # query string
        "features#x",  # fragment
        "features/**",  # recursive glob
        "feat*",  # partial-segment star
        "features//x",  # empty segment
        "features/./x",  # dot segment
        "features/%2e%2e",  # percent-encoded traversal
        "",  # blank
        42,  # not a string
    ],
)
def test_a_refused_pattern_is_logged_and_skipped(pattern, caplog):
    reg = _reg()
    with caplog.at_level("WARNING"):
        reg.register_component("thing-ref", _v, frame="component.html", reads=[pattern, "status"])
    # The component still registers; only the bad pattern is dropped (the good one survives).
    assert reg.components == {"thing-ref": _v}
    assert reg.component_reads == {"thing-ref": ["status"]}
    assert "read" in caplog.text and "refused" in caplog.text


def test_reads_without_a_frame_are_ignored(caplog):
    reg = _reg()
    with caplog.at_level("WARNING"):
        reg.register_component("thing-ref", _v, reads=["features"])
    assert reg.components == {"thing-ref": _v}  # a frame-less kind is still a valid kind
    assert reg.component_reads == {}
    assert "requires frame" in caplog.text


def test_a_bare_string_reads_is_refused_not_iterated_by_character(caplog):
    reg = _reg()
    with caplog.at_level("WARNING"):
        reg.register_component("thing-ref", _v, frame="component.html", reads="features")
    assert reg.component_reads == {}
    assert "list" in caplog.text


def test_reads_on_a_refused_frame_are_dropped_with_the_kind():
    reg = _reg()
    reg.register_component("thing-ref", _v, frame="private.html", reads=["features"])
    assert reg.components == {} and reg.component_reads == {}


# ── FakeRegistry: same refusals, loud fail ───────────────────────────────────────────────


def test_fake_registry_captures_valid_reads():
    reg = FakeRegistry(plugin_id="p", public_paths=PUBLIC)
    reg.register_component("thing-ref", _v, frame="component.html", reads=["features", "features/*"])
    assert reg.component_reads == {"thing-ref": ["features", "features/*"]}


@pytest.mark.parametrize(
    "pattern",
    ["/api/plugins/other/x", "/api/config", "../x", "features?x=1", "features/**", "feat*"],
)
def test_fake_registry_raises_on_a_pattern_the_host_would_refuse(pattern):
    reg = FakeRegistry(plugin_id="p", public_paths=PUBLIC)
    with pytest.raises(ValueError):
        reg.register_component("thing-ref", _v, frame="component.html", reads=[pattern])
    assert reg.components == {} and reg.component_reads == {}


def test_fake_registry_raises_on_reads_without_a_frame():
    reg = FakeRegistry(plugin_id="p", public_paths=PUBLIC)
    with pytest.raises(ValueError):
        reg.register_component("thing-ref", _v, reads=["features"])
    assert reg.components == {}


def test_fake_registry_raises_on_a_bare_string_reads():
    reg = FakeRegistry(plugin_id="p", public_paths=PUBLIC)
    with pytest.raises(ValueError):
        reg.register_component("thing-ref", _v, frame="component.html", reads="features")


# ── loader + catalog: reads ride through to GET /api/components ──────────────────────────

_PLUGIN = '''
def _v(props):
    return None

def register(registry):
    registry.register_component(
        "thing-ref", _v, frame="component.html", reads=["features", "features/*", "../escape"]
    )
    registry.register_component("plain-ref", _v)
'''


def test_loader_and_catalog_carry_reads(tmp_path, monkeypatch):
    d = tmp_path / "p"
    d.mkdir()
    (d / "protoagent.plugin.yaml").write_text(
        "id: p\nname: p plugin\nversion: 0.1.0\nenabled: true\npublic_paths:\n  - /plugins/p/component.html\n",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(_PLUGIN, encoding="utf-8")
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    res = load_plugins(LangGraphConfig())
    assert res.component_frames["thing-ref"] == {
        "plugin": "p",
        "frame_url": "/plugins/p/component.html",
        "reads": ["features", "features/*"],  # the escaping pattern was skipped
    }
    assert res.component_frames["plain-ref"]["reads"] == []

    components.set_plugin_components(res.components)
    components.set_plugin_component_frames(res.component_frames)
    rows = {r["name"]: r for r in components.component_catalog()}
    assert rows["thing-ref"] == {
        "name": "thing-ref",
        "plugin": "p",
        "frame_url": "/plugins/p/component.html",
        "reads": ["features", "features/*"],
    }
    assert rows["plain-ref"]["reads"] == []
    assert rows["table"]["reads"] == []


def test_catalog_never_reports_reads_for_a_frameless_row():
    # Defense in depth: a frame map entry with reads but no frame_url reports none.
    components.set_plugin_components({"thing-ref": _v})
    components.set_plugin_component_frames({"thing-ref": {"plugin": "p", "frame_url": None, "reads": ["x"]}})
    rows = {r["name"]: r for r in components.component_catalog()}
    assert rows["thing-ref"]["reads"] == []
