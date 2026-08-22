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


async def test_respect_disabled_keeps_operator_disable(monkeypatch):
    """UPDATE semantics (#2718): re-installing a bundle must not undo an operator's
    explicit disable — a declared-enable member sitting in plugins.disabled stays
    there; members with no recorded state still get fresh-install enablement."""
    monkeypatch.setattr(
        installer,
        "install",
        lambda url, ref=None, **k: {"bundle": "s", "installed": [{"id": "a"}, {"id": "b"}], "enabled": ["a", "b"]},
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    captured, apply = _capture_apply()

    res = await install_and_activate(
        "https://x", force=True, respect_disabled=True, ctx=_ctx(disabled=["a"]), apply_settings=apply
    )
    assert res.enabled == ["b"]  # a stays off
    assert captured["updates"]["plugins"]["enabled"] == ["b"]
    assert captured["updates"]["plugins"]["disabled"] == ["a"]  # untouched


async def test_update_bundle_repins_and_retires_dropped(monkeypatch):
    from ops.plugins import update_bundle

    monkeypatch.setattr(
        installer,
        "bundle_entry",
        lambda bid: {"id": bid, "source_url": "https://x/stack", "requested_ref": "", "plugins": ["a", "dead"]},
    )
    monkeypatch.setattr(
        installer,
        "install",
        lambda url, ref=None, **k: {"bundle": "s", "installed": [{"id": "a"}], "enabled": ["a"]},
    )
    monkeypatch.setattr(installer, "orphaned_bundle_members", lambda bid, before: ["dead"])
    uninstalled: list[str] = []
    monkeypatch.setattr(installer, "uninstall", lambda pid, **k: uninstalled.append(pid))
    purged: list[str] = []
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: purged.append(pid))

    calls: list = []

    def apply(updates):
        calls.append(updates)
        return True, ["ok"]

    res = await update_bundle("s", ctx=_ctx(), apply_settings=apply)
    assert res.install.enabled == ["a"] and res.install.reloaded is True
    assert res.removed_members == ["dead"] and uninstalled == ["dead"] and "dead" in purged
    assert res.retire_error is None
    # two applies: the activate reload (with a config patch) + the retire pure reload (None)
    assert len(calls) == 2 and calls[1] is None


async def test_update_bundle_release_tag_moves_to_newest(monkeypatch):
    """The headline 'release-tag pin moves to the newest semver tag' was untested
    (the 2732 review: every prior test fed requested_ref='')."""
    from ops.plugins import update_bundle

    monkeypatch.setattr(
        installer,
        "bundle_entry",
        lambda bid: {"id": bid, "source_url": "https://x/stack", "requested_ref": "v1.0.0", "plugins": ["a"]},
    )
    monkeypatch.setattr(installer, "check_plugin_update", lambda entry: {"latest_ref": "v2.0.0"})
    seen: dict = {}

    def fake_install(url, ref=None, **k):
        seen["ref"] = ref
        return {"bundle": "s", "installed": [{"id": "a"}], "enabled": ["a"]}

    monkeypatch.setattr(installer, "install", fake_install)
    monkeypatch.setattr(installer, "orphaned_bundle_members", lambda bid, before: [])
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)

    await update_bundle("s", ctx=_ctx(), apply_settings=lambda u: (True, ["ok"]))
    assert seen["ref"] == "v2.0.0"  # tags are immutable — the recorded one would no-op forever


async def test_update_bundle_explicit_ref_is_never_replaced(monkeypatch):
    """An explicit caller `ref` is a pin request — the newest-tag chase must not
    silently override it (the 2732 review's coderabbit major)."""
    from ops.plugins import update_bundle

    monkeypatch.setattr(
        installer,
        "bundle_entry",
        lambda bid: {"id": bid, "source_url": "https://x/stack", "requested_ref": "v1.0.0", "plugins": ["a"]},
    )

    def boom(entry):
        raise AssertionError("check_plugin_update must not run for an explicit ref")

    monkeypatch.setattr(installer, "check_plugin_update", boom)
    seen: dict = {}

    def fake_install(url, ref=None, **k):
        seen["ref"] = ref
        return {"bundle": "s", "installed": [{"id": "a"}], "enabled": ["a"]}

    monkeypatch.setattr(installer, "install", fake_install)
    monkeypatch.setattr(installer, "orphaned_bundle_members", lambda bid, before: [])
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)

    await update_bundle("s", ref="v1.5.0", ctx=_ctx(), apply_settings=lambda u: (True, ["ok"]))
    assert seen["ref"] == "v1.5.0"


async def test_update_bundle_accumulates_retire_errors(monkeypatch):
    """Retire failures reassigned per member reported only the LAST one (2732 review)."""
    from ops.plugins import update_bundle

    monkeypatch.setattr(
        installer,
        "bundle_entry",
        lambda bid: {"id": bid, "source_url": "https://x/stack", "requested_ref": "", "plugins": ["a", "d1", "d2"]},
    )
    monkeypatch.setattr(
        installer,
        "install",
        lambda url, ref=None, **k: {"bundle": "s", "installed": [{"id": "a"}], "enabled": ["a"]},
    )
    monkeypatch.setattr(installer, "orphaned_bundle_members", lambda bid, before: ["d1", "d2"])

    def failing_uninstall(pid, **k):
        raise installer.InstallError(f"{pid} is stuck")

    monkeypatch.setattr(installer, "uninstall", failing_uninstall)
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)

    res = await update_bundle("s", ctx=_ctx(), apply_settings=lambda u: (True, ["ok"]))
    assert "d1: d1 is stuck" in res.retire_error and "d2: d2 is stuck" in res.retire_error


async def test_update_bundle_unknown_id_raises(monkeypatch):
    from ops.plugins import update_bundle

    monkeypatch.setattr(installer, "bundle_entry", lambda bid: None)
    with pytest.raises(installer.InstallError):
        await update_bundle("ghost", ctx=_ctx(), apply_settings=None)


async def test_uninstall_bundle_op_purges_and_reloads(monkeypatch):
    from ops.plugins import uninstall_bundle

    monkeypatch.setattr(
        installer,
        "uninstall_bundle",
        lambda bid, purge=False: {
            "id": bid,
            "removed_members": ["a"],
            "skipped_missing": [],
            "kept": ["b"],
            "purged": purge,
        },
    )
    purged: list[str] = []
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: purged.append(pid))
    calls: list = []

    def apply(updates):
        calls.append(updates)
        return True, ["ok"]

    rep = await uninstall_bundle("s", ctx=_ctx(), apply_settings=apply)
    assert rep["removed_members"] == ["a"] and rep["kept"] == ["b"]
    assert purged == ["a"] and rep["reloaded"] is True and calls == [None]


async def test_load_failure_lands_in_load_errors(monkeypatch):
    """A plugin that fails to IMPORT is skipped by the loader — the reload still returns
    ok — so the op must read the post-reload roster and carry the failure (#2716).
    Before this, the route toasted "enabled and live" for code that never loaded."""
    from runtime.state import STATE

    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    monkeypatch.setattr(
        STATE,
        "plugin_meta",
        [
            {"id": "demo", "enabled": True, "error": "No module named 'leftpad'"},
            {"id": "other", "enabled": True, "error": "unrelated"},  # not part of this install
        ],
    )

    res = await install_and_activate("https://x", ctx=_ctx(), apply_settings=lambda u: (True, ["ok"]))
    assert res.reloaded is True and res.enabled == ["demo"]
    assert res.load_errors == {"demo": "No module named 'leftpad'"}


async def test_clean_load_reports_no_load_errors(monkeypatch):
    from runtime.state import STATE

    monkeypatch.setattr(installer, "install", lambda url, ref=None, **k: {"id": "demo"})
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    monkeypatch.setattr(STATE, "plugin_meta", [{"id": "demo", "enabled": True}])

    res = await install_and_activate("https://x", ctx=_ctx(), apply_settings=lambda u: (True, ["ok"]))
    assert res.load_errors == {}


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
        "config_inputs:\n"
        "  - { key: board.repo, label: Board repo, type: string, required: true }\n"
        "  - { key: board.auto_merge, label: Auto merge, type: boolean, default: false }\n"
        "  - { key: bogus, label: Bad key }\n"
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


async def test_peek_bundle_refuses_traversal_member_id(tmp_path, monkeypatch):
    """The 2735 review's major: a fetched manifest's member id is untrusted data used
    in a path — `x/../../dir` escaped the TemporaryDirectory before _fetch wrote.
    An unsafe id now yields an error row and NEVER reaches the filesystem/fetch."""
    import shutil
    from pathlib import Path

    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    bundle = tmp_path / "bundle-evil"
    bundle.mkdir()
    (bundle / "protoagent.bundle.yaml").write_text(
        "id: stack\nname: Stack\ndescription: D\n"
        "plugins:\n"
        "  - { id: ../../escape, url: https://example.test/evil }\n"
        "  - { id: fine, url: https://example.test/fine }\n"
    )
    member = tmp_path / "member-fine"
    member.mkdir()
    (member / "protoagent.plugin.yaml").write_text("id: fine\nname: Fine\nversion: 1.0.0\ndescription: ok.\n")
    fetched: list[str] = []

    def fake_fetch(url, ref, dest):
        fetched.append(url)
        shutil.copytree(bundle if url.endswith("/stack-evil") else member, Path(dest))
        return "deadbeef"

    monkeypatch.setattr(installer, "_fetch", fake_fetch)
    result = await plugin_ops.peek_bundle("https://example.test/stack-evil")
    by_id = {m["id"]: m for m in result["members"]}
    assert by_id["../../escape"]["error"] == "invalid member id in manifest"
    assert not any(u.endswith("/evil") for u in fetched)  # the unsafe member never fetched
    assert "error" not in by_id["fine"]  # safe members still peek normally


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


async def test_peek_bundle_surfaces_config_inputs(tmp_path, monkeypatch):
    """The preview exposes the bundle's declared config_inputs (#2934), leniently
    normalized — the un-dotted `bogus` entry drops instead of blanking the preview,
    so the Configure step can render fields WITHOUT installing."""
    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    monkeypatch.setattr(installer, "_fetch", _write_bundle_fixture(tmp_path))
    result = await plugin_ops.peek_bundle("https://example.test/stack-config-inputs")

    assert result["config_inputs"] == [
        {"key": "board.repo", "label": "Board repo", "type": "string", "required": True},
        {"key": "board.auto_merge", "label": "Auto merge", "type": "boolean", "required": False, "default": False},
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
    assert result["mcp"] == [] and result["secrets"] == [] and result["config_inputs"] == []


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


def _host_bundle_lock(tmp_path, monkeypatch):
    """Point the HOST config + lock paths at tmp files carrying a bundle with one
    mcp: template (required ${token} from env/input) — the #2118 seeding fixture."""
    import json

    from graph import config_io
    from graph.plugins import installer as inst

    cfg = tmp_path / "langgraph-config.yaml"
    cfg.write_text("model:\n  name: m\n")
    lock = tmp_path / "plugins.lock"
    lock.write_text(
        json.dumps(
            {
                "plugins": [],
                "bundles": [
                    {
                        "id": "stack",
                        "mcp": [
                            {
                                "template": {
                                    "name": "github",
                                    "transport": "http",
                                    "url": "https://api.githubcopilot.com/mcp/",
                                    "headers": {"Authorization": "Bearer ${token}"},
                                },
                                "inputs": [{"key": "token", "env": "GH_MCP_TOK", "required": True}],
                            }
                        ],
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(config_io, "config_yaml_path", lambda: cfg)
    monkeypatch.setattr(inst, "lock_path", lambda: lock)
    return cfg


async def test_host_bundle_install_seeds_declared_mcp_servers(tmp_path, monkeypatch):
    """#2118 — a bundle installed on the HOST seeds its mcp: templates into the host
    config with the same semantics as workspace create: operator input → enabled;
    unresolved required input → visible-but-inert (enabled: false)."""
    import yaml

    cfg = _host_bundle_lock(tmp_path, monkeypatch)
    monkeypatch.delenv("GH_MCP_TOK", raising=False)
    monkeypatch.setattr(
        installer, "install", lambda url, ref=None, **k: {"bundle": "stack", "installed": [{"id": "a"}], "enabled": []}
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    captured, apply = _capture_apply()

    # No operator input, no env → seeded but disabled (visible-but-inert).
    res = await install_and_activate("https://x/stack", ctx=_ctx(), apply_settings=apply)
    assert res.mcp_seeded == ["github"]
    doc = yaml.safe_load(cfg.read_text())
    by_name = {s["name"]: s for s in doc["mcp"]["servers"]}
    assert by_name["github"]["enabled"] is False and doc["mcp"]["enabled"] is True

    # Operator create-time input → seeded ENABLED, and name-union never clobbers:
    # re-install with an input leaves the existing (disabled) entry alone.
    res2 = await install_and_activate(
        "https://x/stack", ctx=_ctx(), apply_settings=apply, mcp_inputs={"token": "ghp_x"}
    )
    assert res2.mcp_seeded == []  # name already present — config wins, no clobber
    assert yaml.safe_load(cfg.read_text())["mcp"]["servers"][0].get("enabled") is False


async def test_host_bundle_secrets_reach_host_overlay(tmp_path, monkeypatch):
    """#2118 — supplied values for a bundle's DECLARED secrets land in the host
    secrets.yaml (save_secrets path); undeclared keys are ignored."""
    import json

    import yaml

    from graph.plugins import installer as inst

    _host_bundle_lock(tmp_path, monkeypatch)
    lock = inst.lock_path()
    data = json.loads(lock.read_text())
    data["bundles"][0]["secrets"] = [{"key": "api_key", "label": "API Key", "secret": True, "required": True}]
    lock.write_text(json.dumps(data))
    monkeypatch.setattr(
        installer, "install", lambda url, ref=None, **k: {"bundle": "stack", "installed": [{"id": "a"}], "enabled": []}
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    _, apply = _capture_apply()

    await install_and_activate(
        "https://x/stack",
        ctx=_ctx(),
        apply_settings=apply,
        bundle_secrets=[{"key": "api_key", "value": "s3cr3t"}, {"key": "undeclared", "value": "nope"}],
    )
    overlay = yaml.safe_load((tmp_path / "secrets.yaml").read_text())
    assert overlay["stack"]["api_key"] == "s3cr3t"
    assert "undeclared" not in str(overlay)


async def test_host_bundle_install_writes_config_inputs(tmp_path, monkeypatch):
    """#2934 — operator answers to a bundle's declared config_inputs land in the HOST
    config at the declared dotted keys (declared-only; an unanswered input falls back
    to its default), and the written keys ride the result for the route to surface."""
    import json

    import yaml

    from graph import config_io

    cfg = tmp_path / "langgraph-config.yaml"
    cfg.write_text("model:\n  name: m\n")
    lock = tmp_path / "plugins.lock"
    lock.write_text(
        json.dumps(
            {
                "plugins": [],
                "bundles": [
                    {
                        "id": "stack",
                        "config_inputs": [
                            {"key": "board.repo", "label": "Board repo", "type": "string", "required": True},
                            {"key": "board.auto_merge", "label": "Auto merge", "type": "boolean", "default": False},
                        ],
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(config_io, "config_yaml_path", lambda: cfg)
    monkeypatch.setattr(installer, "lock_path", lambda: lock)
    monkeypatch.setattr(
        installer, "install", lambda url, ref=None, **k: {"bundle": "stack", "installed": [{"id": "a"}], "enabled": []}
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)
    _, apply = _capture_apply()

    res = await install_and_activate(
        "https://x/stack",
        ctx=_ctx(),
        apply_settings=apply,
        config_inputs={"board.repo": "org/repo", "not.declared": "nope"},
    )
    assert sorted(res.config_written) == ["board.auto_merge", "board.repo"]  # answer + defaulted toggle
    doc = yaml.safe_load(cfg.read_text())
    assert doc["board"] == {"repo": "org/repo", "auto_merge": False}
    assert "not" not in doc  # undeclared keys never reach the config


async def test_activate_false_skips_bundle_service_seeding(tmp_path, monkeypatch):
    """The CLI's fetch-only install (activate=False) must stay fetch-only — no mcp
    seeding, config untouched (#2118 keeps the ADR 0027 install ≠ enable line)."""
    cfg = _host_bundle_lock(tmp_path, monkeypatch)
    before = cfg.read_text()
    monkeypatch.setattr(
        installer, "install", lambda url, ref=None, **k: {"bundle": "stack", "installed": [{"id": "a"}], "enabled": ["a"]}
    )
    monkeypatch.setattr(loader, "purge_plugin_modules", lambda pid: None)

    res = await install_and_activate("https://x/stack", ctx=_ctx(), activate=False, apply_settings=None)
    assert res.mcp_seeded == [] and res.reloaded is False
    assert cfg.read_text() == before  # not even opened for write


async def test_peek_cache_is_keyed_by_url_and_ref(tmp_path, monkeypatch):
    """A url-only cache key served one ref's preview for every ref of the same bundle
    within the TTL (2735 review) — two refs must peek independently."""
    import ops.plugins as plugin_ops
    from graph.plugins import installer

    plugin_ops._peek_cache.clear()
    fixture = _write_bundle_fixture(tmp_path)
    calls: list = []

    def counting_fetch(url, ref, dest):
        calls.append((url, ref))
        return fixture(url, ref, dest)

    monkeypatch.setattr(installer, "_fetch", counting_fetch)
    await plugin_ops.peek_bundle("https://example.test/stack")
    n_first = len(calls)
    await plugin_ops.peek_bundle("https://example.test/stack")  # same (url, ref) → cached
    assert len(calls) == n_first
    await plugin_ops.peek_bundle("https://example.test/stack", ref="v2")  # new ref → fresh peek
    assert len(calls) > n_first


# ── _install_bundle member semver chase (#2960 S3) ────────────────────────────
# A bundle update re-pins every member to the manifest's ref — which DOWNGRADED a
# member an operator had force-installed ahead of the archetype's pin. Release-tag
# members now ls-remote their own repo (via check_plugin_update) and install the
# newest semver tag when one exists; SHA/branch refs and builtins are untouched.


def _member_chase_env(tmp_path, monkeypatch):
    """Sandbox the lock + capture install() calls for direct _install_bundle tests."""
    monkeypatch.setattr(installer, "lock_path", lambda: tmp_path / "plugins.lock")
    calls: list[tuple] = []

    def fake_install(url, ref=None, **k):
        calls.append((url, ref))
        return {"id": url.rsplit("/", 1)[-1]}

    monkeypatch.setattr(installer, "install", fake_install)
    return calls


def _stack(members):
    return {"id": "stack", "name": "Stack", "plugins": members}


def test_bundle_member_release_tag_chases_newest_semver(tmp_path, monkeypatch):
    """The manifest pins v1.0.0 but a newer tag exists on the member repo — the
    bundle install must NOT downgrade the member; it installs the newest tag."""
    calls = _member_chase_env(tmp_path, monkeypatch)
    checked: list[dict] = []

    def fake_check(entry):
        checked.append(entry)
        return {"latest_ref": "v1.2.0", "latest_sha": "b" * 40, "behind": True}

    monkeypatch.setattr(installer, "check_plugin_update", fake_check)

    res = installer._install_bundle(
        _stack([{"id": "board", "url": "https://x/board", "ref": "v1.0.0"}]),
        "https://x/stack",
        "a" * 40,
        None,
        force=True,
        by="test",
        allow=None,
    )
    assert calls == [("https://x/board", "v1.2.0")]  # chased tag, not the manifest pin
    # the check ran against the MEMBER repo with the manifest pin as the baseline
    assert checked == [{"id": "board", "source_url": "https://x/board", "requested_ref": "v1.0.0", "resolved_sha": ""}]
    assert [p["id"] for p in res["installed"]] == ["board"]


def test_bundle_member_no_newer_tag_uses_manifest_pin(tmp_path, monkeypatch):
    calls = _member_chase_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        installer, "check_plugin_update", lambda entry: {"latest_ref": None, "latest_sha": None, "behind": False}
    )

    installer._install_bundle(
        _stack([{"id": "board", "url": "https://x/board", "ref": "v1.0.0"}]),
        "https://x/stack",
        "a" * 40,
        None,
        force=True,
        by="test",
        allow=None,
    )
    assert calls == [("https://x/board", "v1.0.0")]


def test_bundle_member_chase_failure_falls_back_to_manifest_pin(tmp_path, monkeypatch):
    """A network error / timeout in the chase must not fail the bundle install —
    the member falls back to the manifest pin (the pre-#2960 behavior)."""
    calls = _member_chase_env(tmp_path, monkeypatch)

    def boom(entry):
        raise RuntimeError("network down")

    monkeypatch.setattr(installer, "check_plugin_update", boom)

    installer._install_bundle(
        _stack([{"id": "board", "url": "https://x/board", "ref": "v1.0.0"}]),
        "https://x/stack",
        "a" * 40,
        None,
        force=True,
        by="test",
        allow=None,
    )
    assert calls == [("https://x/board", "v1.0.0")]


def test_bundle_member_non_release_refs_pass_through(tmp_path, monkeypatch):
    """SHA pins, branch refs, and ref-less members never trigger the network chase."""
    calls = _member_chase_env(tmp_path, monkeypatch)

    def boom(entry):
        raise AssertionError("check_plugin_update must not run for a non-release-tag ref")

    monkeypatch.setattr(installer, "check_plugin_update", boom)
    sha = "0123456789abcdef0123456789abcdef01234567"

    installer._install_bundle(
        _stack(
            [
                {"id": "a", "url": "https://x/a", "ref": sha},
                {"id": "b", "url": "https://x/b", "ref": "main"},
                {"id": "c", "url": "https://x/c"},
            ]
        ),
        "https://x/stack",
        "a" * 40,
        None,
        force=True,
        by="test",
        allow=None,
    )
    assert calls == [("https://x/a", sha), ("https://x/b", "main"), ("https://x/c", None)]


def test_bundle_member_builtin_skips_fetch_and_chase(tmp_path, monkeypatch):
    calls = _member_chase_env(tmp_path, monkeypatch)

    def boom(entry):
        raise AssertionError("check_plugin_update must not run for a builtin member")

    monkeypatch.setattr(installer, "check_plugin_update", boom)

    res = installer._install_bundle(
        _stack(
            [
                {"id": "delegates", "builtin": True, "ref": "v1.0.0"},
                {"id": "board", "url": "https://x/board", "ref": "main"},
            ]
        ),
        "https://x/stack",
        "a" * 40,
        None,
        force=True,
        by="test",
        allow=None,
    )
    assert res["skipped_builtin"] == ["delegates"]
    assert calls == [("https://x/board", "main")]
