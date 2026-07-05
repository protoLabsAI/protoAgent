"""ops.plugins.install_and_activate (ADR 0075 D2) — install + auto-enable/hot-reload as one
op the REST route, a future `protoagent plugin install`, and the operator MCP share.
``installer.install`` and the live-agent apply are faked here (the real install is covered by
test_plugin_installer_*); these test the op's orchestration + the injected-applier seam."""

from __future__ import annotations

import types

import pytest

from graph.plugins import installer, loader
from ops import OpContext, registry
from ops.plugins import install_and_activate


def _ctx(enabled=(), disabled=()):
    cfg = types.SimpleNamespace(plugins_enabled=list(enabled), plugins_disabled=list(disabled))
    return OpContext(knowledge_store=None, graph_config=cfg)


def _capture_apply():
    captured: dict = {}

    def _apply(updates):
        captured["updates"] = updates
        return True, ["reloaded"]

    return captured, _apply


async def test_install_single_auto_enables_and_purges(monkeypatch):
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    purged: list[str] = []
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: purged.append(pid))
    captured, apply = _capture_apply()

    res = await install_and_activate("https://x", ctx=_ctx(enabled=["delegates"]), apply_settings=apply)
    assert res.enabled == ["demo"] and res.reloaded is True and res.enable_error is None
    assert res.installed_ids == ["demo"] and purged == ["demo"]  # re-exec-on-reload purge ran
    assert set(captured["updates"]["plugins"]["enabled"]) == {"delegates", "demo"}


async def test_install_bundle_enables_declared_members(monkeypatch):
    monkeypatch.setattr(
        installer,
        "install",
        lambda url, ref=None, **k: {"bundle": "s", "installed": [{"id": "a"}, {"id": "b"}], "enabled": ["a"]},
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    captured, apply = _capture_apply()

    res = await install_and_activate("https://x", ctx=_ctx(), apply_settings=apply)
    assert res.enabled == ["a"] and captured["updates"]["plugins"]["enabled"] == ["a"]
    assert res.installed_ids == ["a", "b"]  # both members fetched to disk


async def test_activate_false_installs_only(monkeypatch):
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    captured, apply = _capture_apply()

    res = await install_and_activate("https://x", activate=False, ctx=_ctx(), apply_settings=apply)
    assert res.enabled == [] and res.reloaded is False and "updates" not in captured  # applier not called


async def test_no_applier_installs_only(monkeypatch):
    """A disk-only caller (a CLI with no running server) passes apply_settings=None."""
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)

    res = await install_and_activate("https://x", ctx=_ctx(), apply_settings=None)
    assert res.enabled == [] and res.reloaded is False and res.installed_ids == ["demo"]


async def test_reload_failure_surfaces_enable_error(monkeypatch):
    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)

    res = await install_and_activate(
        "https://x", ctx=_ctx(), apply_settings=lambda u: (False, ["graph compile failed"])
    )
    assert res.reloaded is False and res.enabled == [] and "graph compile failed" in res.enable_error


async def test_bundle_config_overlay_seeds_only_unset(monkeypatch):
    monkeypatch.setattr(
        installer,
        "install",
        lambda url, ref=None, **k: {
            "bundle": "s",
            "installed": [{"id": "browser"}],
            "enabled": ["browser"],
            "config": {"browser": {"panel_mode": "full", "timeout": 30}},
        },
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    import graph.config_io as cio

    monkeypatch.setattr(cio, "load_yaml_doc", lambda p=None: {"browser": {"panel_mode": "compact"}})
    captured, apply = _capture_apply()

    await install_and_activate("https://x", ctx=_ctx(), apply_settings=apply)
    assert captured["updates"]["browser"] == {"timeout": 30}  # operator's panel_mode not clobbered


async def test_install_error_propagates(monkeypatch):
    def _boom(url, ref=None, **k):
        raise installer.InstallError("bad source")

    monkeypatch.setattr(installer, "install", _boom)
    with pytest.raises(installer.InstallError):
        await install_and_activate("https://x", ctx=_ctx(), apply_settings=lambda u: (True, []))


def test_op_registered_as_mutating():
    assert registry()["plugins.install_and_activate"].mutates is True  # never in safe-operator
