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


# ── peek_bundle (archetype preview) ───────────────────────────────────────────


def _write_bundle_fixture(tmp_path):
    import shutil
    from pathlib import Path

    member = tmp_path / "member-src"
    (member / "skills" / "demo").mkdir(parents=True)
    (member / "protoagent.plugin.yaml").write_text(
        "id: m1\nname: Member One\nversion: 1.2.3\ndescription: A member.\nrequires_pip: [somelib]\n"
    )
    (member / "skills" / "demo" / "SKILL.md").write_text("---\nname: demo\ndescription: Demo skill.\n---\n\nBody.\n")

    bundle = tmp_path / "bundle-src"
    bundle.mkdir()
    (bundle / "protoagent.bundle.yaml").write_text(
        "id: stack\nname: Stack\ndescription: D\nverified_against: 0.1.0\n"
        "plugins:\n"
        "  - { id: hello, builtin: true }\n"
        "  - { id: m1, url: https://example.test/m1, ref: v1.2.3 }\n"
        "enabled: [hello, m1]\n"
        "mcp:\n"
        "  - { template: github, inputs: [ { key: token, label: GitHub Token, secret: true, required: true } ] }\n"
        "secrets:\n"
        "  - { key: openai_api_key, label: OpenAI API Key, placeholder: 'sk-...', secret: true, required: true }\n"
    )

    def fake_fetch(url, ref, dest):
        src = member if url.rstrip("/").endswith("/m1") else bundle
        shutil.copytree(src, Path(dest))
        return "deadbeef"

    return fake_fetch


async def test_peek_bundle_enumerates_members(tmp_path, monkeypatch):
    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    monkeypatch.setattr(installer, "_fetch", _write_bundle_fixture(tmp_path))
    result = await plugin_ops.peek_bundle("https://example.test/stack")

    assert result["kind"] == "bundle" and result["id"] == "stack"
    by_id = {m["id"]: m for m in result["members"]}
    assert by_id["hello"]["builtin"] is True and "error" not in by_id["hello"]
    m1 = by_id["m1"]
    assert m1["version"] == "1.2.3" and m1["requires_pip"] == ["somelib"]
    assert m1["skills"] == [{"name": "demo", "description": "Demo skill."}]


async def test_peek_bundle_survives_unreachable_member(tmp_path, monkeypatch):
    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    fixture = _write_bundle_fixture(tmp_path)

    def flaky_fetch(url, ref, dest):
        if "example.test/m1" in url:
            raise RuntimeError("clone failed")
        return fixture(url, ref, dest)

    monkeypatch.setattr(installer, "_fetch", flaky_fetch)
    result = await plugin_ops.peek_bundle("https://example.test/stack2")
    m1 = next(m for m in result["members"] if m["id"] == "m1")
    assert "clone failed" in m1["error"]
    hello = next(m for m in result["members"] if m["id"] == "hello")
    assert "error" not in hello


async def test_peek_bundle_surfaces_mcp_and_secrets(tmp_path, monkeypatch):
    """The preview exposes the bundle's MCP inputs + declared secrets so the
    ArchetypePreviewDialog can show what this archetype will ask for (#2041)."""
    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    monkeypatch.setattr(installer, "_fetch", _write_bundle_fixture(tmp_path))
    result = await plugin_ops.peek_bundle("https://example.test/stack-inputs")

    assert result["mcp"] == [
        {
            "template": "github",
            "inputs": [{"key": "token", "label": "GitHub Token", "secret": True, "required": True}],
        }
    ]
    assert result["secrets"] == [
        {
            "key": "openai_api_key",
            "label": "OpenAI API Key",
            "placeholder": "sk-...",
            "secret": True,
            "required": True,
        }
    ]


async def test_peek_single_plugin_reports_empty_mcp_and_secrets(tmp_path, monkeypatch):
    """A non-bundle (single-plugin repo) peek still carries the keys, both empty —
    so the dialog reads mcp/secrets uniformly across bundle + plugin previews."""
    import shutil
    from pathlib import Path

    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    plugin_src = tmp_path / "plugin-src"
    plugin_src.mkdir()
    (plugin_src / "protoagent.plugin.yaml").write_text(
        "id: solo\nname: Solo\nversion: 0.1.0\ndescription: A lone plugin.\n"
    )

    def fake_fetch(url, ref, dest):
        shutil.copytree(plugin_src, Path(dest))
        return "cafef00d"

    monkeypatch.setattr(installer, "_fetch", fake_fetch)
    result = await plugin_ops.peek_bundle("https://example.test/solo")

    assert result["kind"] == "plugin"
    assert result["mcp"] == [] and result["secrets"] == []


async def test_peek_bundle_caches_by_url(tmp_path, monkeypatch):
    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    calls = {"n": 0}
    fixture = _write_bundle_fixture(tmp_path)

    def counting_fetch(url, ref, dest):
        calls["n"] += 1
        return fixture(url, ref, dest)

    monkeypatch.setattr(installer, "_fetch", counting_fetch)
    await plugin_ops.peek_bundle("https://example.test/stack3")
    first = calls["n"]
    await plugin_ops.peek_bundle("https://example.test/stack3")
    assert calls["n"] == first, "second peek must hit the TTL cache"
