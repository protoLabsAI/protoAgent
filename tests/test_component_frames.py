"""Frame-declaring plugin components (ADR 0118 D5, #4087): a kind may render in a
plugin-served PAGE instead of a compiled console renderer. ``register_component(name,
validator, frame=...)`` accepts a path relative to ``/plugins/<id>`` ONLY when it is safe
(no leading ``/``, no ``..``) and exposed by the manifest's ``public_paths`` — an
opaque-origin frame iframe carries no bearer, so the page must be auth-exempt.

This card is the registry + testkit half; a later card carries the stored frame through the
loader and GET /api/components."""

from __future__ import annotations

from pathlib import Path

import pytest

from graph.plugins.registry import PluginRegistry
from graph.plugins.testkit import FakeRegistry

PUBLIC = ["/plugins/p/component.html", "/plugins/p/view"]


def _v(props):
    return None


# ── PluginRegistry: warn + skip (degrade-safe live) ──────────────────────────────────────


def test_a_valid_public_frame_is_accepted_and_stored():
    reg = PluginRegistry("p", Path("/tmp"), public_paths=PUBLIC)
    reg.register_component("thing-ref", _v, frame="component.html")
    assert reg.components == {"thing-ref": _v}
    assert reg.component_frames == {"thing-ref": "component.html"}


def test_a_frame_under_a_public_subtree_is_accepted():
    # public_paths entries are auth-exempt PREFIXES, so a page beneath one is public too.
    reg = PluginRegistry("p", Path("/tmp"), public_paths=["/plugins/p/ui/"])
    reg.register_component("thing-ref", _v, frame="ui/widget.html")
    assert reg.component_frames == {"thing-ref": "ui/widget.html"}


def test_two_arg_calls_register_no_frame():
    # Existing behavior: a component with no frame registers the validator and stores nothing.
    reg = PluginRegistry("p", Path("/tmp"), public_paths=PUBLIC)
    reg.register_component("thing-ref", _v)
    assert reg.components == {"thing-ref": _v}
    assert reg.component_frames == {}


def test_absolute_frame_is_refused(caplog):
    reg = PluginRegistry("p", Path("/tmp"), public_paths=["/plugins/p/etc/passwd"])
    with caplog.at_level("WARNING"):
        reg.register_component("thing-ref", _v, frame="/etc/passwd")
    assert reg.components == {}  # whole registration skipped, like a bad name
    assert reg.component_frames == {}
    assert "frame" in caplog.text


def test_parent_traversal_frame_is_refused():
    reg = PluginRegistry("p", Path("/tmp"), public_paths=["/plugins/p/"])
    reg.register_component("thing-ref", _v, frame="../other/secret.html")
    assert reg.components == {} and reg.component_frames == {}


def test_frame_not_in_public_paths_is_refused():
    reg = PluginRegistry("p", Path("/tmp"), public_paths=PUBLIC)
    reg.register_component("thing-ref", _v, frame="private.html")
    assert reg.components == {} and reg.component_frames == {}


def test_no_public_paths_refuses_every_frame():
    reg = PluginRegistry("p", Path("/tmp"))  # host didn't wire public_paths
    reg.register_component("thing-ref", _v, frame="component.html")
    assert reg.components == {} and reg.component_frames == {}


def test_a_bad_name_is_still_refused_before_the_frame_is_examined():
    reg = PluginRegistry("p", Path("/tmp"), public_paths=["/plugins/p/table"])
    reg.register_component("table", _v, frame="table")  # core kind — refused
    assert reg.components == {} and reg.component_frames == {}


# ── FakeRegistry: same validation, loud fail ─────────────────────────────────────────────


def test_fake_registry_accepts_and_stores_a_valid_frame():
    reg = FakeRegistry(plugin_id="p", public_paths=PUBLIC)
    reg.register_component("thing-ref", _v, frame="component.html")
    assert reg.components == {"thing-ref": _v}
    assert reg.component_frames == {"thing-ref": "component.html"}


def test_fake_registry_two_arg_call_is_unchanged():
    reg = FakeRegistry(plugin_id="p")
    reg.register_component("thing-ref", _v)
    assert reg.components == {"thing-ref": _v}
    assert reg.component_frames == {}


@pytest.mark.parametrize(
    "frame, public_paths",
    [
        ("/etc/passwd", ["/plugins/p/etc/passwd"]),  # absolute
        ("../secret.html", ["/plugins/p/"]),  # parent traversal
        ("private.html", PUBLIC),  # not listed in public_paths
        ("component.html", []),  # no public paths wired at all
    ],
)
def test_fake_registry_raises_on_a_frame_the_host_would_refuse(frame, public_paths):
    reg = FakeRegistry(plugin_id="p", public_paths=public_paths)
    with pytest.raises(ValueError):
        reg.register_component("thing-ref", _v, frame=frame)
    assert reg.components == {} and reg.component_frames == {}
