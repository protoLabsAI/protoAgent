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

from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins
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


# ── loader wiring: the production registry actually sees the manifest's public_paths ──────
# The unit tests above pass public_paths by hand; they stay green even if load_plugins builds
# the registry without them. These exercise the real loader so a frame that the manifest
# exposes survives end-to-end (and one it doesn't is still refused + its kind dropped).

_FRAME_PLUGIN = '''
def _v(props):
    return None

def register(registry):
    registry.register_component("thing-ref", _v, frame="component.html")
'''


def _make_frame_plugin(root: Path, pid: str, *, public_paths: list[str]) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    pp = "".join(f"  - {p}\n" for p in public_paths)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid} plugin\nversion: 0.1.0\nenabled: true\npublic_paths:\n{pp}",
        encoding="utf-8",
    )
    (d / "__init__.py").write_text(_FRAME_PLUGIN, encoding="utf-8")


def test_loader_passes_manifest_public_paths_so_a_frame_component_loads(tmp_path, monkeypatch):
    # The frame lives under a declared public_path → the component survives load_plugins.
    _make_frame_plugin(tmp_path, "p", public_paths=["/plugins/p/component.html"])
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    res = load_plugins(LangGraphConfig())
    assert "thing-ref" in res.components  # not refused + dropped as it was before the wiring


def test_loader_drops_a_frame_component_whose_manifest_omits_the_public_path(tmp_path, monkeypatch):
    # Manifest exposes a DIFFERENT page, so this frame is genuinely not public → still refused,
    # taking the whole kind with it (same log-and-skip as a bad name).
    _make_frame_plugin(tmp_path, "p", public_paths=["/plugins/p/other.html"])
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    res = load_plugins(LangGraphConfig())
    assert "thing-ref" not in res.components
