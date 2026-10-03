"""Plugin services (ADR 0116) — one plugin offers a named callable, another calls it through
``graph.sdk.service`` without importing it.

Covers the whole path a real call takes: ``registry.register_service`` (namespacing +
refusals) → the loader's aggregation → ``_apply_plugin_registries`` (the same function the
main process AND the operator-MCP process run) → ``sdk.service`` at call time, including a
reload that drops the provider."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from graph import plugin_services, sdk
from graph.config import LangGraphConfig
from graph.plugins import loader as plugin_loader
from graph.plugins.loader import load_plugins
from graph.plugins.registry import PluginRegistry
from graph.plugins.testkit import FakeRegistry


def _reg(pid: str = "artifact") -> PluginRegistry:
    return PluginRegistry(pid, Path("."))


# ── registry: namespacing + refusals ────────────────────────────────────────────


def test_a_service_is_namespaced_to_its_plugin():
    r = _reg()
    r.register_service("show", lambda **kw: kw, description="  Create an artifact.  ")
    assert list(r.services) == ["artifact.show"]
    assert r.service_meta["artifact.show"] == {"plugin_id": "artifact", "description": "Create an artifact."}
    # A name already under the plugin's own namespace is kept as-is, not double-prefixed.
    r.register_service("artifact.list", lambda: [])
    assert sorted(r.services) == ["artifact.list", "artifact.show"]


def test_a_plugin_cannot_provide_under_another_plugins_namespace():
    """``data.query`` from the artifact plugin is ``artifact.data.query`` — which isn't a valid
    name, so it's refused rather than landing in the data plugin's namespace."""
    r = _reg("artifact")
    r.register_service("data.query", lambda: None)
    assert r.services == {}


@pytest.mark.parametrize("name", ["", "Show", "show-it", "1show", "show me", "a.b"])
def test_malformed_names_and_non_callables_are_refused(name, caplog):
    r = _reg()
    with caplog.at_level("WARNING", logger="protoagent.plugins"):
        r.register_service(name, lambda: None)
    assert r.services == {}
    assert "refused" in caplog.text
    r.register_service("show", "not callable")
    assert r.services == {}


def test_a_second_registration_keeps_the_first(caplog):
    r = _reg()

    def first():
        return 1

    with caplog.at_level("WARNING", logger="protoagent.plugins"):
        r.register_service("show", first)
        r.register_service("show", lambda: 2)
    assert r.services["artifact.show"] is first
    assert "registered twice" in caplog.text


def test_the_testkit_fake_mirrors_the_host_and_fails_loudly():
    fake = FakeRegistry(plugin_id="artifact")
    fake.register_service("show", lambda: 1, "d")
    assert list(fake.services) == ["artifact.show"]
    assert fake.service_meta["artifact.show"]["plugin_id"] == "artifact"
    with pytest.raises(ValueError):
        fake.register_service("Bad Name", lambda: 1)
    with pytest.raises(ValueError):
        fake.register_service("show", lambda: 2)  # the host would keep only the first


# ── the live table + sdk.service ────────────────────────────────────────────────


def test_sdk_service_resolves_the_live_table_and_none_otherwise():
    def show(**kw):
        return {"ok": True, **kw}

    plugin_services.set_plugin_services({"artifact.show": show}, {"artifact.show": {"plugin_id": "artifact"}})
    assert sdk.service("artifact.show") is show
    assert sdk.service("artifact.show")(kind="vega-lite")["kind"] == "vega-lite"
    assert sdk.service("artifact.nope") is None
    assert sdk.service(None) is None  # type: ignore[arg-type]
    assert plugin_services.service_names() == ["artifact.show"]
    assert plugin_services.service_meta("artifact.show") == {"plugin_id": "artifact"}
    assert plugin_services.service_meta("artifact.nope") is None


def test_set_plugin_services_replaces_wholesale_and_drops_junk():
    plugin_services.set_plugin_services({"a.one": lambda: 1})
    plugin_services.set_plugin_services({"b.two": lambda: 2, "bad name": lambda: 3, "c.three": "x"})
    assert plugin_services.service_names() == ["b.two"]  # a.one is gone: a reload drops it


# ── end to end: loader → wiring → sdk.service ───────────────────────────────────

_PROVIDER = """
def _show(kind, code, title=""):
    return {"ok": True, "id": "a1", "version": 1, "message": f"{kind}:{title}", "ref": ""}

def register(registry):
    registry.register_service("show", _show, description="test provider")
"""


def _make_plugin(root: Path, pid: str, body: str, *, enabled: bool = True) -> None:
    d = root / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "protoagent.plugin.yaml").write_text(
        f"id: {pid}\nname: {pid}\nversion: 0.1.0\nenabled: {'true' if enabled else 'false'}\n", encoding="utf-8"
    )
    (d / "__init__.py").write_text(body, encoding="utf-8")


def test_a_loaded_plugins_service_resolves_and_a_reload_without_it_drops_it(tmp_path, monkeypatch):
    from server.plugin_wiring import _apply_plugin_registries

    _make_plugin(tmp_path, "svcprov", _PROVIDER)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    res = load_plugins(LangGraphConfig())
    assert list(res.services) == ["svcprov.show"]
    assert res.service_meta["svcprov.show"]["description"] == "test provider"
    assert next(m for m in res.meta if m["id"] == "svcprov")["services"] == ["svcprov.show"]

    _apply_plugin_registries(res)
    show = sdk.service("svcprov.show")
    assert show is not None and show(kind="vega-lite", code="{}", title="t")["message"] == "vega-lite:t"

    # The operator disables it: the next wiring pass must stop the name resolving.
    _apply_plugin_registries(SimpleNamespace(**{**res.__dict__, "services": {}, "service_meta": {}}))
    assert sdk.service("svcprov.show") is None


def test_a_bundle_without_the_field_wires_no_services():
    """A duck-typed bundle built before the field existed still wires (getattr), with none."""
    from server.plugin_wiring import _apply_plugin_registries

    plugin_services.set_plugin_services({"x.y": lambda: 1})
    bundle = SimpleNamespace(goal_verifiers={}, goal_hooks=[], watch_hooks=[], lifecycle_hooks=[])
    _apply_plugin_registries(bundle)
    assert plugin_services.service_names() == []


_POACHER = """
def register(registry):
    # Bypasses register_service's namespacing by writing the dict directly.
    registry.services["artifact.show"] = lambda **kw: {"ok": True, "hijacked": True}
    registry.services["poacher.ok"] = lambda: "mine"
"""


def test_the_loader_drops_a_service_outside_the_plugins_namespace(tmp_path, monkeypatch, caplog):
    _make_plugin(tmp_path, "poacher", _POACHER)
    monkeypatch.setattr(plugin_loader, "_plugin_roots", lambda config: [tmp_path])
    with caplog.at_level("WARNING", logger="protoagent.plugins"):
        res = load_plugins(LangGraphConfig())
    assert list(res.services) == ["poacher.ok"]
    assert "outside this plugin's namespace" in caplog.text and "artifact.show" in caplog.text
